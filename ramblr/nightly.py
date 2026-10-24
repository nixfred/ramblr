"""The ONE nightly batch pass (ramblr-nightly.timer). Transcription stays live; this pass does the rest per day:
  1. idea backstop: closes stale spoken ideas and queues any spoken idea that still has no storyboard
  2. daily summary + action items, in the same model call(s)
  3. ask-your-transcript index catch-up (embeddings on the NPU)
  4. encrypted backup of the DB to whi
Nothing here deletes anything. Re-running a day adds a summary VERSION and only new action items.

  python -m ramblr.nightly                 yesterday plus any recent day still missing a summary, then index + backup
  python -m ramblr.nightly --day 2026-10-24 [--force] [--no-backup] [--no-index]
"""
import argparse
import hashlib
import json
import logging
import re
import socket
import sys
import time
from datetime import date, datetime, timedelta

from . import config, dates, db as dbm, ideas, llm, notify, storyboard

log = logging.getLogger("ramblr.nightly")

PROMPT = """You read one block of a room transcript from Fred's home office and return ONE JSON object, nothing else:
{
 "headline": "one sentence: what this part of the day was about",
 "decisions": ["a decision that was actually made, with who made it"],
 "todos": ["short to-do lines (the same things as action_items, in plain words)"],
 "who_said": [{"speaker": "name exactly as in the transcript", "points": ["what they said or argued, briefly"]}],
 "topics": ["short topic labels"],
 "action_items": [{"owner": "who will do it (speaker name, a named person, or '' if unclear)",
                   "task": "imperative, under 15 words",
                   "specifics": "details that matter: what, where, amounts, names",
                   "due_phrase": "the exact words about timing ('tomorrow', 'by Friday') or ''",
                   "due_date": "YYYY-MM-DD resolved against the DATE of that line, or null",
                   "line_ids": [ids of the transcript lines it comes from]}]
}
Rules:
- Only real commitments or requests: someone says they will do something, or asks someone to do it. Not wishes, not ideas, not things already done.
- Remarks addressed to Larry (the assistant) are notes, never action items.
- 'I'll ...' from a speaker means the owner is that speaker. 'Speaker N' names are unknown voices: keep them as written.
- line_ids MUST be the #numbers shown at the start of the lines. Never invent ids.
- Resolve due_date from the line's own date (e.g. 'tomorrow' said on 2026-10-24 is 2026-10-25). null if no timing was said.
- Empty lists are fine. Do not pad. No markdown, no code fence.

TRANSCRIPT (#id [date time] speaker: text):
{lines}
"""

MERGE = """Merge these partial summaries of ONE day (in order) into one daily recap. Return ONE JSON object only:
{"headline": "one or two sentences for the whole day", "decisions": [...], "todos": [...],
 "who_said": [{"speaker": "...", "points": ["..."]}], "topics": [...]}
Keep every real decision and to-do; merge duplicates; at most 6 points per speaker.

PARTIALS:
{parts}
"""


def day_lines(db, day):
    rows = db.execute("""SELECT s.id, s.ts, s.text, COALESCE(sp.name,'Unknown') AS speaker FROM segments s
                         LEFT JOIN speakers sp ON sp.id=s.speaker_id
                         WHERE s.day=? AND s.noise=0 AND COALESCE(s.media,0) NOT IN (1,2) ORDER BY s.ts, s.id""",
                      (day,)).fetchall()
    return [dict(r) for r in rows]


def fmt_line(l):
    return f"#{l['id']} [{datetime.fromtimestamp(l['ts']).strftime('%Y-%m-%d %H:%M')}] {l['speaker']}: {l['text']}"


def blocks(lines, budget):
    out, cur, size = [], [], 0
    for l in lines:
        s = len(fmt_line(l)) + 1
        if cur and size + s > budget:
            out.append(cur)
            cur, size = [], 0
        cur.append(l)
        size += s
    if cur:
        out.append(cur)
    return out


def ask_json(prompt, keys):
    """Cheapest generator first; a reply that is not the JSON we asked for gets one retry on the fallback model."""
    attempts = [(config.NIGHTLY_GEN, config.NIGHTLY_CLAUDE_MODEL), ("claude", config.NIGHTLY_FALLBACK_MODEL)]
    last = None
    for mode, model in attempts:
        try:
            text, gen, used, secs, note = llm.generate(prompt, mode=mode, claude_model=model, timeout=config.NIGHTLY_TIMEOUT)
            obj = storyboard.parse_json(text)
            missing = [k for k in keys if k not in obj]
            if missing:
                raise ValueError(f"missing keys {missing}")
            return obj, gen, used, secs, note
        except Exception as e:
            last = e
            log.warning("nightly model attempt (%s/%s) failed: %s", mode, model, str(e)[:200])
    raise RuntimeError(f"no usable model output: {last}")


def norm_task(t):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", t.lower()).split())


def store_items(db, day, items, block_ids, ts_of, model):
    """Insert validated action items. Idempotent: stable key, and an item whose source lines overlap an existing
    item with the same owner on that day is treated as the same item (re-runs with different wording)."""
    new = []
    existing = [(json.loads(r["line_ids"]), r["owner"].lower()) for r in
                db.execute("SELECT line_ids, owner FROM action_items WHERE day=?", (day,))]
    for it in items or []:
        if not isinstance(it, dict):
            continue
        task = " ".join(str(it.get("task", "")).split())[:200]
        ids = []
        for x in it.get("line_ids") or []:
            try:
                ids.append(int(str(x).lstrip("#")))
            except ValueError:
                pass
        ids = sorted({i for i in ids if i in block_ids})
        if not task or not ids:
            continue           # an item that cannot be traced to a real line is dropped, never guessed
        owner = " ".join(str(it.get("owner") or "").split())[:60]
        if any(set(ids) & set(e_ids) and owner.lower() == e_owner for e_ids, e_owner in existing):
            continue
        said = ts_of[ids[0]]
        phrase = " ".join(str(it.get("due_phrase") or "").split())[:80]
        due = dates.resolve(phrase, said)
        if due == dates.SAME_WEEKDAY:      # ambiguous: trust the model's reading of context, else next week
            due = dates.valid_iso(it.get("due_date"), said) or (date.fromtimestamp(said) + timedelta(7)).isoformat()
        due = due or dates.valid_iso(it.get("due_date"), said)
        key = hashlib.sha1(f"{day}|{ids[0]}|{norm_task(task)}".encode()).hexdigest()[:20]
        cur = db.execute("""INSERT OR IGNORE INTO action_items(key,day,owner,task,specifics,due,due_phrase,line_ids,source_ts,created,model)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                         (key, day, owner, task, " ".join(str(it.get("specifics") or "").split())[:500], due, phrase,
                          json.dumps(ids), said, time.time(), model))
        if cur.rowcount:
            new.append(cur.lastrowid)
            existing.append((ids, owner.lower()))
            from . import events
            events.emit(db, "action_item.new", {"id": cur.lastrowid, "owner": owner, "task": task, "due": due, "day": day, "line_ids": ids})
    return new


def summarize_day(db, day, force=False):
    lines = day_lines(db, day)
    if not lines:
        return {"day": day, "skipped": "no lines"}
    last = db.execute("SELECT version, lines FROM summaries WHERE day=? ORDER BY version DESC LIMIT 1", (day,)).fetchone()
    if last and last["lines"] == len(lines) and not force:
        return {"day": day, "skipped": "summary already covers every line"}
    ts_of = {l["id"]: l["ts"] for l in lines}
    parts, new_items, gens, secs_total, notes = [], [], set(), 0.0, []
    from . import conversations
    cards = [c["card"] for c in conversations.cards_for_day(db, day) if c["card"]]
    card_txt = ""
    if cards:      # B3-7: the day's end-of-conversation cards feed the recap
        card_txt = "CONVERSATION CARDS ALREADY WRITTEN TODAY (use them for the recap; action items still come only from the lines):\n" + \
                   "\n".join(f"- {c.get('title', '')}: {c.get('summary', '')}" for c in cards) + "\n\n"
    for i, blk in enumerate(blocks(lines, config.NIGHTLY_BLOCK_CHARS)):
        prompt = PROMPT.replace("{lines}", (card_txt if i == 0 else "") + "\n".join(fmt_line(l) for l in blk))
        obj, gen, model, secs, note = ask_json(prompt, ["headline", "action_items"])
        gens.add(f"{gen}:{model}")
        secs_total += secs
        notes.append(note)
        new_items += store_items(db, day, obj.get("action_items"), {l["id"] for l in blk}, ts_of, model)
        parts.append({k: obj.get(k, []) for k in ("headline", "decisions", "todos", "who_said", "topics")})
    if len(parts) == 1:
        body = parts[0]
    else:
        body, gen, model, secs, note = ask_json(MERGE.replace("{parts}", json.dumps(parts, indent=1)), ["headline"])
        gens.add(f"{gen}:{model}")
        secs_total += secs
    body["action_item_ids"] = [r[0] for r in db.execute("SELECT id FROM action_items WHERE day=? ORDER BY source_ts", (day,))]
    v = (last["version"] if last else 0) + 1
    gen_s = ",".join(sorted(gens))
    db.execute("INSERT INTO summaries(day,version,created,generator,model,latency_s,lines,body) VALUES(?,?,?,?,?,?,?,?)",
               (day, v, time.time(), gen_s.split(":")[0], gen_s, round(secs_total, 2), len(lines), json.dumps(body)))
    return {"day": day, "version": v, "lines": len(lines), "new_action_items": len(new_items), "model": gen_s,
            "seconds": round(secs_total, 1), "notes": [n for n in notes if n]}


def idea_backstop(db):
    changed = ideas.scan(db)
    queued = []
    for r in db.execute("""SELECT i.id FROM ideas i WHERE i.open=0 AND i.source IN ('trigger','iphone') AND i.status!='dismissed'
                           AND i.build_requested=0 AND NOT EXISTS (SELECT 1 FROM storyboards s WHERE s.idea_id=i.id)""").fetchall():
        db.execute("UPDATE ideas SET build_requested=1 WHERE id=?", (r["id"],))
        queued.append(r["id"])
    if queued:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            s.sendto(b'{"t":"build"}', str(config.IDEAS_SOCK))
            s.close()
        except OSError:
            pass
    for iid in set(changed) | set(queued):
        notify.send({"t": "idea", "id": iid})
    return {"changed": sorted(changed), "queued_builds": queued}


def run_day(db, day, force=False):
    rid = db.execute("INSERT INTO nightly_runs(day,started) VALUES(?,?)", (day, time.time())).lastrowid
    out = {"day": day}
    try:
        out["summary"] = summarize_day(db, day, force)
        try:
            from . import memory
            out["memory"] = memory.extract_day(db, day)          # B3-8/9: durable facts and goals
        except Exception as e:
            log.exception("facts/goals for %s failed", day)
            out["memory"] = {"error": str(e)[:200]}
        status = "ok"
    except Exception as e:
        log.exception("nightly pass for %s failed", day)
        out["error"] = str(e)[:300]
        status = "error"
    db.execute("UPDATE nightly_runs SET finished=?, status=?, detail=? WHERE id=?", (time.time(), status, json.dumps(out), rid))
    notify.send({"t": "day", "day": day})
    return out


def days_to_run(db, today):
    """Yesterday, plus up to a week back any day that has lines but no summary (missed nights)."""
    yday = (today - timedelta(1)).isoformat()
    week = (today - timedelta(7)).isoformat()
    rows = db.execute("""SELECT DISTINCT day FROM segments WHERE noise=0 AND day>=? AND day<? AND
                         day NOT IN (SELECT day FROM summaries)""", (week, today.isoformat())).fetchall()
    return sorted({yday} | {r[0] for r in rows})


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--day")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--no-index", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stdout)
    db = dbm.connect(config.DB_PATH)
    report = {"started": datetime.now().isoformat(timespec="seconds"), "ideas": idea_backstop(db), "days": []}
    for d in ([a.day] if a.day else days_to_run(db, date.today())):
        report["days"].append(run_day(db, d, a.force))
    try:
        from . import folders
        report["folders"] = folders.file_unsorted(db)          # B3-11: unfiled ideas, one call
    except Exception as e:
        log.exception("folder filing failed")
        report["folders"] = {"error": str(e)[:200]}
    try:
        from . import insights
        report["patterns"] = insights.weekly_patterns(db)       # B3-9: once per ISO week
    except Exception as e:
        log.exception("weekly patterns failed")
        report["patterns"] = {"error": str(e)[:200]}
    if not a.no_index:
        from . import search
        try:
            report["index"] = search.index_pending(db)
        except Exception as e:
            log.exception("index catch-up failed")
            report["index"] = {"error": str(e)[:300]}
    if not a.no_index:
        try:
            from . import ideatime
            report["ideas_over_time"] = ideatime.run(db)          # B4-2 heat, B4-8 lineage (needs the fresh index)
        except Exception as e:
            log.exception("idea heat/lineage failed")
            report["ideas_over_time"] = {"error": str(e)[:200]}
    if not a.no_backup and config.BACKUP_HOST:
        from . import backup
        try:
            report["backup"] = backup.run(db)
        except Exception as e:
            log.exception("backup failed")
            report["backup"] = {"error": str(e)[:300]}
    try:
        from . import health
        health.storage(db)            # records tonight's DB-size snapshot for the real growth figure
    except Exception:
        log.exception("db size snapshot failed")
    dbm.set_meta(db, "nightly_last", json.dumps(report))
    print(json.dumps(report, indent=1))
    return 0 if all("error" not in d for d in report["days"]) and "error" not in report.get("backup", {}) else 1


if __name__ == "__main__":
    sys.exit(main())
