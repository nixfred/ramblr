"""Long-term facts (B3-8) and goals (B3-9), kept from what is said in the room.

The nightly pass asks the cheap model once per day for durable facts (about people, places, preferences, plans,
things owned) and stated goals, each tied to the lines it came from. A fact that says the same thing as one
already kept (same subject, NPU-embedding cosine >= 0.9, or the same normalised text) only bumps its mention count
and last-seen date; a new fact can mark an older one as superseded. Nothing is deleted: Fred can reject a fact
or a goal (a status), and goals can be marked done."""
import hashlib
import json
import logging
import re
import time
from datetime import datetime

from . import config, db as dbm, llm, storyboard

log = logging.getLogger("ramblr.memory")
DUP = 0.9

PROMPT = """From this day of room transcript (Fred's home office), pull out ONLY durable things worth remembering for
months. Return ONE JSON object only:
{"facts": [{"subject": "person/place/thing it is about", "fact": "one plain sentence", "kind": "person|preference|plan|place|thing|work|health|other",
            "supersedes": "an older fact this replaces, or ''", "line_ids": [ids]}],
 "goals": [{"owner": "who", "goal": "what they want to achieve", "by": "deadline words or ''", "line_ids": [ids]}]}
Rules: skip small talk, one-off chores (those are action items), jokes and anything said by the TV. A fact must be
true beyond today (e.g. "Sam's mother visits every Thanksgiving", "Fred prefers TypeScript over Python").
line_ids must be the #numbers shown. Empty lists are fine.
TRANSCRIPT (#id [date time] speaker: text):
{lines}
"""


def norm(t):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", t.lower()).split())


def _vec(text):
    try:
        from .search import embedder
        return embedder().embed("search_document: " + text)
    except Exception:
        return None


def store(db, day, obj, ts_of, model):
    import numpy as np
    out = {"facts_new": 0, "facts_bumped": 0, "goals_new": 0}
    valid = set(ts_of)
    for f in obj.get("facts") or []:
        fact, subj = " ".join(str(f.get("fact", "")).split())[:300], " ".join(str(f.get("subject", "")).split())[:80]
        ids = sorted({int(str(x).lstrip("#")) for x in f.get("line_ids") or [] if str(x).lstrip("#").isdigit()} & valid)
        if not fact or not ids:
            continue
        v = _vec(fact)
        dup = None
        for r in db.execute("SELECT id, fact, vec FROM facts WHERE lower(subject)=lower(?) AND status='active'", (subj,)):
            if norm(r["fact"]) == norm(fact) or (v is not None and r["vec"] and float(np.dot(dbm.blob_to_vec(r["vec"]), v)) >= DUP):
                dup = r["id"]
                break
        if dup:
            old = json.loads(db.execute("SELECT line_ids FROM facts WHERE id=?", (dup,)).fetchone()[0])
            db.execute("UPDATE facts SET mentions=mentions+1, last_seen=?, line_ids=? WHERE id=?",
                       (day, json.dumps(sorted(set(old) | set(ids))[-50:]), dup))
            out["facts_bumped"] += 1
            continue
        key = hashlib.sha1(f"{norm(subj)}|{norm(fact)}".encode()).hexdigest()[:20]
        cur = db.execute("""INSERT OR IGNORE INTO facts(key, subject, fact, kind, first_seen, last_seen, mentions, line_ids, status, vec, model, created)
                            VALUES(?,?,?,?,?,?,1,?,'active',?,?,?)""",
                         (key, subj, fact, str(f.get("kind") or "other")[:20], day, day, json.dumps(ids),
                          dbm.vec_to_blob(v) if v is not None else None, model, time.time()))
        if cur.rowcount:
            out["facts_new"] += 1
            sup = norm(str(f.get("supersedes") or ""))
            if sup:
                for r in db.execute("SELECT id, fact FROM facts WHERE lower(subject)=lower(?) AND status='active' AND id<>?", (subj, cur.lastrowid)):
                    if norm(r["fact"]) == sup or (sup and sup in norm(r["fact"])):
                        db.execute("UPDATE facts SET status='superseded', superseded_by=? WHERE id=?", (cur.lastrowid, r["id"]))
    for g in obj.get("goals") or []:
        goal = " ".join(str(g.get("goal", "")).split())[:300]
        ids = sorted({int(str(x).lstrip("#")) for x in g.get("line_ids") or [] if str(x).lstrip("#").isdigit()} & valid)
        if not goal or not ids:
            continue
        key = hashlib.sha1(f"{norm(str(g.get('owner', '')))}|{norm(goal)}".encode()).hexdigest()[:20]
        cur = db.execute("""INSERT OR IGNORE INTO goals(key, owner, goal, by_phrase, first_seen, last_seen, mentions, line_ids, status, created)
                            VALUES(?,?,?,?,?,?,1,?,'open',?)""",
                         (key, str(g.get("owner") or "")[:60], goal, str(g.get("by") or "")[:60], day, day, json.dumps(ids), time.time()))
        if cur.rowcount:
            out["goals_new"] += 1
        else:
            db.execute("UPDATE goals SET mentions=mentions+1, last_seen=? WHERE key=?", (day, key))
    return out


def extract_day(db, day):
    from .nightly import day_lines, fmt_line, blocks
    lines = day_lines(db, day)
    if len(lines) < 5:
        return {"day": day, "skipped": "too few lines"}
    if dbm.get_meta(db, f"memory_done:{day}") == str(len(lines)):
        return {"day": day, "skipped": "already read"}
    ts_of = {l["id"]: l["ts"] for l in lines}
    total = {"facts_new": 0, "facts_bumped": 0, "goals_new": 0}
    for blk in blocks(lines, config.NIGHTLY_BLOCK_CHARS):
        text, gen, model, secs, note = llm.generate(PROMPT.replace("{lines}", "\n".join(fmt_line(l) for l in blk)), mode=config.NIGHTLY_GEN,
                                                    claude_model=config.NIGHTLY_CLAUDE_MODEL, timeout=config.NIGHTLY_TIMEOUT)
        obj = storyboard.parse_json(text)
        for k, v in store(db, day, obj, {l["id"]: l["ts"] for l in blk}, model).items():
            total[k] += v
    dbm.set_meta(db, f"memory_done:{day}", len(lines))
    return {"day": day, **total}
