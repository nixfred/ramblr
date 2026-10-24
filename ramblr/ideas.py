"""Idea records: spoken-trigger detection, ranges chosen on the site, and which ideas are due for a storyboard.
Dismiss is only a status change. There is no delete anywhere."""
import hashlib
import re
import time

from . import config, db as dbm

TRIGGER = re.compile(config.IDEA_TRIGGER, re.I)
WAKE = re.compile(r"\blarry\b", re.I)
END = re.compile(config.IDEA_END, re.I)


def _title(text):
    t = " ".join(text.split())
    return (t[:57] + "...") if len(t) > 60 else t


def create(db, seg_ids, source, key=None):
    """Create (or return the existing) idea for these lines. Returns (idea_id, created)."""
    ids = sorted({int(i) for i in seg_ids})
    if not ids:
        raise ValueError("no lines")
    ph = ",".join("?" * len(ids))
    rows = db.execute(f"SELECT id, ts, text FROM segments WHERE id IN ({ph}) ORDER BY ts, id", ids).fetchall()
    if len(rows) != len(ids):
        raise ValueError("unknown line id")
    key = key or "range:" + hashlib.sha1(",".join(map(str, ids)).encode()).hexdigest()[:16]
    now = time.time()
    db.execute("BEGIN IMMEDIATE")
    try:
        ex = db.execute("SELECT id FROM ideas WHERE key=?", (key,)).fetchone()
        if ex:
            db.execute("COMMIT")
            return ex["id"], False
        iid = db.execute("INSERT INTO ideas(key,title,source,open,created,updated,last_line_ts) VALUES(?,?,?,?,?,?,?)",
                         (key, _title(rows[0]["text"]), source, 1 if source == "trigger" else 0, now, now, rows[-1]["ts"])).lastrowid
        db.executemany("INSERT INTO idea_lines(idea_id,segment_id) VALUES(?,?)", [(iid, r["id"]) for r in rows])
        from . import events
        events.emit(db, "idea.new", {"idea_id": iid, "title": _title(rows[0]["text"]), "source": source, "lines": len(rows)})
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return iid, True


def scan(db, now=None):
    """Walk lines newer than the last scan: a trigger phrase opens an idea, following lines join it until
    'that's it' or a pause longer than IDEA_GAP. Returns ids of ideas created or changed."""
    now = now or time.time()
    last = int(dbm.get_meta(db, "idea_scan_id", "0"))
    changed = set()
    # media lines (TV, music, a movie) never open or join an idea; NULL = not decided yet = talk
    rows = db.execute("""SELECT id, ts, dur, text, diar, manual, speaker_id, source FROM segments WHERE id>? AND noise=0
                         AND COALESCE(media,0) NOT IN (1,2) AND COALESCE(source,'') NOT IN ('Zoom','Wispr') ORDER BY id""",
                      (last,)).fetchall()
    from . import me
    for r in rows:
        op = db.execute("SELECT id, last_line_ts FROM ideas WHERE open=1 ORDER BY id DESC LIMIT 1").fetchone()
        if op and r["ts"] - op["last_line_ts"] > config.IDEA_GAP:
            db.execute("UPDATE ideas SET open=0 WHERE id=?", (op["id"],))
            changed.add(op["id"])
            op = None
        if op:
            db.execute("INSERT OR IGNORE INTO idea_lines(idea_id,segment_id) VALUES(?,?)", (op["id"], r["id"]))
            closing = 1 if END.search(r["text"]) else 0
            db.execute("UPDATE ideas SET last_line_ts=?, updated=?, open=? WHERE id=?",
                       (r["ts"] + r["dur"], time.time(), 0 if closing else 1, op["id"]))
            changed.add(op["id"])
        elif TRIGGER.search(r["text"]):
            if WAKE.search(r["text"]):                 # B4-5: only Fred's voice wakes "Larry"
                g = me.gate(db, r, now)
                if g == "wait":
                    break                              # voice not identified yet: try again next pass
                if g == "no":
                    dbm.set_meta(db, "idea_scan_id", r["id"])
                    continue
            iid, _ = create(db, [r["id"]], "trigger", key=f"trigger:{r['id']}")
            db.execute("UPDATE ideas SET last_line_ts=? WHERE id=?", (r["ts"] + r["dur"], iid))
            changed.add(iid)
        dbm.set_meta(db, "idea_scan_id", r["id"])
    for op in db.execute("SELECT id, last_line_ts FROM ideas WHERE open=1").fetchall():
        if now - op["last_line_ts"] > config.IDEA_GAP:
            db.execute("UPDATE ideas SET open=0 WHERE id=?", (op["id"],))
            changed.add(op["id"])
    return changed


def due(db, now=None):
    """Ideas needing a build now: explicitly requested, or a closed spoken idea past the quiet period with no storyboard."""
    now = now or time.time()
    out = []
    for r in db.execute("""SELECT i.id, i.build_requested, i.source, i.last_line_ts, i.status,
                                  (SELECT COUNT(*) FROM storyboards s WHERE s.idea_id=i.id) AS nsb
                           FROM ideas i WHERE i.open=0 ORDER BY i.id""").fetchall():
        if r["build_requested"] or (r["source"] in ("trigger", "iphone") and r["nsb"] == 0 and r["status"] != "dismissed"
                                    and now - r["last_line_ts"] >= config.IDEA_QUIET):
            out.append(r["id"])
    return out
