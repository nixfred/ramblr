"""Pattern insights (B3-9), topic trackers and talk stats (B3-10). All numbers come straight from the DB; only the
weekly "patterns" paragraph is written by the cheap model, from those numbers (never from raw transcript)."""
import hashlib
import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from . import config, db as dbm, llm, storyboard

ROOM = "s.noise=0 AND COALESCE(s.media,0) NOT IN (1,2)"


def talk_stats(db, days=30):
    """Per person: talk time, lines, words/min, longest monologue, busiest hour; plus the room's hour histogram."""
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = db.execute(f"""SELECT s.ts, s.dur, s.text, s.speaker_id, COALESCE(sp.name,'Unknown') name, sp.named FROM segments s
                          LEFT JOIN speakers sp ON sp.id=s.speaker_id WHERE s.day>=? AND {ROOM} ORDER BY s.ts""", (since,)).fetchall()
    per = defaultdict(lambda: {"secs": 0.0, "lines": 0, "words": 0, "hours": Counter(), "longest": 0.0, "run": 0.0})
    hours = Counter()
    last_spk, last_end = None, 0
    for r in rows:
        p = per[r["name"] if r["named"] else "Unknown voices"]
        p["secs"] += r["dur"]; p["lines"] += 1; p["words"] += len(r["text"].split())
        h = datetime.fromtimestamp(r["ts"]).hour
        p["hours"][h] += r["dur"]; hours[h] += r["dur"]
        # a monologue = the same voice line after line with short pauses
        if r["speaker_id"] == last_spk and r["ts"] - last_end < 8:
            p["run"] += r["dur"]
        else:
            p["run"] = r["dur"]
        p["longest"] = max(p["longest"], p["run"])
        last_spk, last_end = r["speaker_id"], r["ts"] + r["dur"]
    out = []
    for name, p in sorted(per.items(), key=lambda x: -x[1]["secs"]):
        out.append({"name": name, "talk_min": round(p["secs"] / 60, 1), "lines": p["lines"],
                    "wpm": round(p["words"] / (p["secs"] / 60), 0) if p["secs"] > 30 else None,
                    "longest_min": round(p["longest"] / 60, 1), "busiest_hour": p["hours"].most_common(1)[0][0] if p["hours"] else None})
    return {"days": days, "people": out, "hours": [round(hours.get(h, 0) / 60, 1) for h in range(24)]}


def tracker_counts(db, tracker, days=30):
    """Mentions per day of a tracked topic: FTS5 phrase/words match over room lines."""
    fq = _fq(tracker)
    if not fq:
        return []
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    per = {r[0]: r[1] for r in db.execute(f"""SELECT s.day, COUNT(*) FROM segments_fts f JOIN segments s ON s.id=f.rowid
                                              WHERE segments_fts MATCH ? AND s.day>=? AND {ROOM} GROUP BY s.day""", (fq, since))}
    return [{"day": (datetime.now() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d"),
             "n": per.get((datetime.now() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d"), 0)} for i in range(days)]


def energy_map(db, days=90):
    """B4-12: when ideas happen, weekday x hour, from each idea's first spoken line (or its creation time)."""
    since = time.time() - days * 86400
    grid = [[0] * 24 for _ in range(7)]
    for r in db.execute("""SELECT COALESCE((SELECT MIN(s.ts) FROM idea_lines il JOIN segments s ON s.id=il.segment_id WHERE il.idea_id=i.id), i.created) t
                           FROM ideas i WHERE i.created>=?""", (since,)):
        t = datetime.fromtimestamp(r["t"])
        grid[t.weekday()][t.hour] += 1
    total = sum(map(sum, grid))
    best = max(((d, h) for d in range(7) for h in range(24)), key=lambda x: grid[x[0]][x[1]]) if total else None
    return {"grid": grid, "total": total, "peak": {"weekday": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][best[0]], "hour": best[1]} if best else None}


def _fq(t):
    from .search import fts_query
    return '"' + t["query"].replace('"', "") + '"' if t["phrase"] else fts_query(t["query"])


def trackers(db, days=30):
    out = []
    for r in db.execute("SELECT * FROM topic_trackers WHERE active=1 ORDER BY id"):
        series = tracker_counts(db, dict(r), days)
        fq = _fq(r)
        last = db.execute(f"""SELECT s.id, s.day, s.text FROM segments_fts f JOIN segments s ON s.id=f.rowid
                              WHERE segments_fts MATCH ? AND {ROOM} ORDER BY s.ts DESC LIMIT 1""", (fq,)).fetchone() if fq else None
        out.append({"id": r["id"], "name": r["name"], "query": r["query"], "phrase": bool(r["phrase"]), "series": series,
                    "total": sum(x["n"] for x in series), "last": dict(last) if last else None})
    return out


def add_tracker(db, name, query, phrase=False):
    name, query = " ".join(name.split())[:60], " ".join(query.split())[:120]
    if not name or not query:
        raise ValueError("name and query required")
    db.execute("INSERT INTO topic_trackers(name, query, phrase, active, created) VALUES(?,?,?,1,?)", (name, query, 1 if phrase else 0, time.time()))


PATTERN_PROMPT = """Here are numbers about the last {days} days of talk in Fred's home office (computed from the transcript,
TV excluded). Write 3 to 6 short, specific pattern insights as ONE JSON object only: {"insights": ["..."]}.
Only state what the numbers support (times of day, who talks when, which topics rise or fall, when ideas happen,
how many action items stay open). No advice unless it follows directly from a number.
NUMBERS:
{numbers}
"""


def weekly_patterns(db, force=False):
    """Once a week (nightly pass on Mondays, or on demand): numbers -> a few plain insights, kept forever."""
    wk = datetime.now().strftime("%G-W%V")
    if not force and db.execute("SELECT 1 FROM insights WHERE week=?", (wk,)).fetchone():
        return {"week": wk, "skipped": "already written"}
    ts = talk_stats(db, 28)
    ideas_h = Counter(datetime.fromtimestamp(r[0]).hour for r in db.execute("SELECT created FROM ideas WHERE created>?", (time.time() - 28 * 86400,)))
    numbers = {"talk": ts["people"], "talk_minutes_by_hour": ts["hours"],
               "ideas_by_hour": dict(ideas_h), "open_action_items": db.execute("SELECT COUNT(*) FROM action_items WHERE sent=0").fetchone()[0],
               "topics": [{"name": t["name"], "last_28_days": t["total"], "last_7_days": sum(x["n"] for x in t["series"][-7:])} for t in trackers(db, 28)]}
    if not ts["people"]:
        return {"week": wk, "skipped": "no talk yet"}
    text, gen, model, secs, note = llm.generate(PATTERN_PROMPT.replace("{days}", "28").replace("{numbers}", json.dumps(numbers)),
                                                mode=config.NIGHTLY_GEN, claude_model=config.NIGHTLY_CLAUDE_MODEL, timeout=config.NIGHTLY_TIMEOUT)
    obj = storyboard.parse_json(text)
    db.execute("INSERT INTO insights(week, body, numbers, model, created) VALUES(?,?,?,?,?)",
               (wk, json.dumps(obj.get("insights", [])[:8]), json.dumps(numbers), model, time.time()))
    return {"week": wk, "insights": len(obj.get("insights", []))}
