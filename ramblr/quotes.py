"""Podcast prep quote pull (B4-11): the best verbatim lines from a time range or about a topic.

Candidates come from the selected range (or, for a topic, the B3-3 hybrid search). The cheap model picks up to 10
quotes and says why each works; every pick is checked against the real line text and dropped unless it is
verbatim (a quote must appear in its line, ignoring case and punctuation at the edges). Results are kept forever
(quote_pulls) with speaker, time, a link and a ▶ clip for lines under 24 h old, and export as Markdown."""
import json
import re
import time
from datetime import datetime

from . import config, llm, storyboard

PROMPT = """Pick the best quotes for a podcast or talk from these transcript lines (Fred's home office). Return ONE JSON
object only: {"title": "short title for this pull", "quotes": [{"line_id": 123, "quote": "exact words copied from that line",
"why": "why it works (insight, story, punch, surprise)"}]}
Rules: up to 10 quotes; copy words EXACTLY from the line (you may trim the start or end of a line, never change
words); prefer vivid, quotable, self-contained lines; skip filler. {focus}
LINES (#id [date time] speaker: text):
{lines}
"""


def _norm(t):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", t.lower())).strip()


def pull(db, start=None, end=None, topic="", limit_lines=400):
    from . import search
    if topic:
        ids, h = search.hybrid(db, topic, k=40)
    elif start and end:
        ids = [r[0] for r in db.execute("SELECT id FROM segments WHERE ts>=? AND ts<=? AND noise=0 AND COALESCE(media,0) NOT IN (1,2) ORDER BY ts LIMIT ?",
                                        (start, end, limit_lines))]
    else:
        raise ValueError("give a time range or a topic")
    if not ids:
        raise LookupError("no lines there")
    ph = ",".join("?" * len(ids))
    rows = {r["id"]: dict(r) for r in db.execute(f"""SELECT s.id, s.ts, s.day, s.text, COALESCE(sp.name,'Unknown') AS speaker FROM segments s
                                                    LEFT JOIN speakers sp ON sp.id=s.speaker_id WHERE s.id IN ({ph}) ORDER BY s.ts""", ids)}
    lines = "\n".join(f"#{r['id']} [{datetime.fromtimestamp(r['ts']).strftime('%Y-%m-%d %H:%M')}] {r['speaker']}: {r['text']}" for r in rows.values())
    text, gen, model, secs, note = llm.generate(PROMPT.replace("{focus}", f"Focus on: {topic}." if topic else "").replace("{lines}", lines[-50000:]),
                                                mode=config.NIGHTLY_GEN, claude_model=config.ASK_CLAUDE_MODEL, timeout=config.NIGHTLY_TIMEOUT)
    obj = storyboard.parse_json(text)
    out = []
    for q in obj.get("quotes") or []:
        try:
            lid = int(str(q.get("line_id", "")).lstrip("#"))
        except ValueError:
            continue
        r = rows.get(lid)
        quote = " ".join(str(q.get("quote", "")).split())
        if not r or not quote or _norm(quote) not in _norm(r["text"]):
            continue                         # not verbatim: dropped
        out.append({"line_id": lid, "quote": quote, "why": str(q.get("why", ""))[:200], "speaker": r["speaker"], "ts": r["ts"], "day": r["day"],
                    "playable": config.AUDIO24_ENABLED and time.time() - r["ts"] < 86400})
        if len(out) >= 10:
            break
    pid = db.execute("INSERT INTO quote_pulls(title, topic, start, end, quotes, model, created) VALUES(?,?,?,?,?,?,?)",
                     (str(obj.get("title") or topic or "Quote pull")[:120], topic[:200], start, end, json.dumps(out), f"{gen}:{model}", time.time())).lastrowid
    return {"id": pid, "title": obj.get("title"), "quotes": out, "dropped": len(obj.get("quotes") or []) - len(out)}


def markdown(db, pid):
    r = db.execute("SELECT * FROM quote_pulls WHERE id=?", (pid,)).fetchone()
    if not r:
        raise LookupError("no such pull")
    out = [f"# {r['title']}", ""]
    for q in json.loads(r["quotes"]):
        out += [f"> {q['quote']}", ">", f"> — {q['speaker']}, {q['day']} {datetime.fromtimestamp(q['ts']).strftime('%H:%M')}", "",
                f"_{q['why']}_" if q["why"] else "", ""]
    return "\n".join(out)
