"""Ideas over time: heat scoring (B4-2), lineage of repeat pitches (B4-8), weekly review (B4-7).

Every idea gets an NPU embedding of its title, pitch and spoken lines. Each night:
- mentions: room-talk chunks after the idea was born whose meaning matches it (cosine >= MENTION) count as the
  idea coming up again; the newest one is its last mention.
- heat = (storyboard score, or 5) x (1 + ln(1 + mentions)) x 0.5^(days since it was last touched / HALF_LIFE).
  Touched = created, mentioned again, built, status changed or DO IT. Ignored ideas fade; repeated ones climb.
- lineage: two ideas with cosine >= LINK are the same pitch coming back; the link is kept (never deleted) and the
  card shows "pitched before".
The weekly review lists Later and Dismissed ideas from the last 4 weeks by heat, with Revive."""
import json
import math
import time
from datetime import datetime

import numpy as np

from . import db as dbm

MENTION = 0.72
LINK = 0.80
HALF_LIFE = 14.0


def idea_text(db, iid):
    r = db.execute("SELECT title FROM ideas WHERE id=?", (iid,)).fetchone()
    sb = db.execute("SELECT body FROM storyboards WHERE idea_id=? AND status='ok' ORDER BY version DESC LIMIT 1", (iid,)).fetchone()
    body = json.loads(sb["body"]) if sb else {}
    lines = " ".join(x[0] for x in db.execute("""SELECT s.text FROM idea_lines il JOIN segments s ON s.id=il.segment_id
                                                 WHERE il.idea_id=? ORDER BY s.ts LIMIT 40""", (iid,)))
    return " ".join(filter(None, [body.get("title") or (r["title"] if r else ""), body.get("pitch", ""), lines]))[:2000], body


def embed_all(db, embed):
    n = 0
    for r in db.execute("SELECT id FROM ideas WHERE vec IS NULL OR vec_rev IS NOT (SELECT COUNT(*) FROM storyboards s WHERE s.idea_id=ideas.id)").fetchall():
        text, _ = idea_text(db, r["id"])
        if text:
            v = embed("search_document: " + text)
            rev = db.execute("SELECT COUNT(*) FROM storyboards WHERE idea_id=?", (r["id"],)).fetchone()[0]
            db.execute("UPDATE ideas SET vec=?, vec_rev=? WHERE id=?", (dbm.vec_to_blob(v), rev, r["id"]))
            n += 1
    return n


def score(db, now=None):
    now = now or time.time()
    ideas = db.execute("SELECT id, created, updated, vec, status FROM ideas WHERE vec IS NOT NULL").fetchall()
    if not ideas:
        return {"ideas": 0}
    chunks = db.execute("SELECT id, ts0, seg_ids, vec FROM chunks ORDER BY id").fetchall()
    M = np.stack([dbm.blob_to_vec(c["vec"]) for c in chunks]) if chunks else np.zeros((0, 768), np.float32)
    ts = np.array([c["ts0"] for c in chunks]) if chunks else np.zeros(0)
    own = {r["id"]: {x[0] for x in db.execute("SELECT segment_id FROM idea_lines WHERE idea_id=?", (r["id"],))} for r in ideas}
    vecs = {r["id"]: dbm.blob_to_vec(r["vec"]) for r in ideas}
    out = {"ideas": len(ideas), "links": 0}
    for r in ideas:
        v = vecs[r["id"]]
        mentions, last = 0, None
        if len(M):
            sims = M @ v
            for i in np.where((sims >= MENTION) & (ts > r["created"] + 600))[0]:
                if not set(json.loads(chunks[i]["seg_ids"])) & own[r["id"]]:
                    mentions += 1
                    last = max(last or 0, ts[i])
        sb = db.execute("SELECT body FROM storyboards WHERE idea_id=? AND status='ok' ORDER BY version DESC LIMIT 1", (r["id"],)).fetchone()
        base = float((json.loads(sb["body"]).get("score") or {}).get("value") or 5) if sb else 5.0
        touched = max(r["created"], r["updated"] or 0, last or 0)
        age = max(0.0, (now - touched) / 86400)
        heat = round(base * (1 + math.log1p(mentions)) * 0.5 ** (age / HALF_LIFE), 2)
        db.execute("UPDATE ideas SET heat=?, mentions=?, last_mention=? WHERE id=?", (heat, mentions, last, r["id"]))
    ids = sorted(vecs)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            s = float(vecs[a] @ vecs[b])
            if s >= LINK:
                cur = db.execute("INSERT OR IGNORE INTO idea_links(a, b, score, created) VALUES(?,?,?,?)", (a, b, round(s, 3), now))
                out["links"] += cur.rowcount
    return out


def run(db):
    from .search import embedder
    e = embedder()
    return {"embedded": embed_all(db, e.embed), **score(db)}


def lineage(db, iid):
    rows = db.execute("""SELECT CASE WHEN a=? THEN b ELSE a END AS other, score FROM idea_links WHERE a=? OR b=? ORDER BY score DESC""",
                      (iid, iid, iid)).fetchall()
    out = []
    for r in rows:
        i = db.execute("SELECT id, title, status, created FROM ideas WHERE id=?", (r["other"],)).fetchone()
        if i:
            out.append({"id": i["id"], "title": i["title"], "status": i["status"], "created": i["created"], "similarity": r["score"]})
    return out


def review(db, weeks=4):
    since = time.time() - weeks * 7 * 86400
    return [dict(r) for r in db.execute("""SELECT id, title, status, heat, mentions, last_mention, created FROM ideas
                                           WHERE status IN ('later','dismissed') AND (updated>=? OR created>=? OR last_mention>=?)
                                           ORDER BY COALESCE(heat,0) DESC LIMIT 50""", (since, since, since))]
