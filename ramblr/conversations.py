"""Conversations, the rolling live summary and end-of-conversation cards (B3-7).

A conversation is a run of room lines (TV/music excluded). It ends after a silence of CONV_GAP seconds, or when
the people change out completely (nobody from the last few minutes speaks again and new voices take over).
While a conversation is open, a short rolling summary is refreshed at most every LIVE_EVERY seconds once a few new
lines have come in. When it closes, a summary card is written (title, summary, decisions, action items, who was
there): saved forever, shown on the timeline, its action items go into the action-item list, and the nightly
recap reads the day's cards. Model calls go through llm.generate (local Ollama only when the GPU is idle, else
the logged-in Claude CLI on the cheap tier)."""
import json
import logging
import os
import time

from . import config, db as dbm, llm, notify, storyboard

log = logging.getLogger("ramblr.conversations")
CONV_GAP = float(os.environ.get("RAMBLR_CONV_GAP", 600))           # 10 min of silence ends a conversation
TURNOVER = float(os.environ.get("RAMBLR_CONV_TURNOVER", 300))      # look-back for "the people changed out"
MIN_LINES = int(os.environ.get("RAMBLR_CONV_MIN_LINES", 4))         # shorter exchanges get no card
LIVE_EVERY = float(os.environ.get("RAMBLR_LIVE_EVERY", 180))        # rolling summary at most every 3 min
LIVE_MIN_NEW = int(os.environ.get("RAMBLR_LIVE_MIN_NEW", 3))

LIVE_PROMPT = """You keep a rolling live summary of a conversation happening right now in Fred's home office.
Return ONE JSON object only: {"headline": "one short line: what they are talking about right now",
"bullets": ["3 to 5 short bullets of the main points so far, newest last"], "open": ["questions still open, if any"]}
Be concrete; no filler; never invent anything not in the lines. Lines (#id [time] speaker: text):
{lines}
"""

# B3-6 summary templates by context. Override or add in ~/.config/ramblr/templates.json (same shape).
TEMPLATES = {
    "work meeting": {"when": "work, projects, code, clients, planning with colleagues or bots",
                     "sections": {"decisions": "decisions made, with who", "next steps": "what happens next and who owns it",
                                  "risks": "risks, blockers, open questions"}},
    "family logistics": {"when": "household, kids, pets, errands, appointments, money at home",
                         "sections": {"plans": "what is planned and when", "reminders": "things to remember",
                                      "who does what": "who took which job"}},
    "solo ramble": {"when": "one person thinking out loud",
                    "sections": {"ideas": "ideas worth keeping", "todos": "things they said they would do",
                                 "threads": "topics they kept returning to"}},
    "phone call": {"when": "one side of a call, someone not in the room",
                   "sections": {"who and why": "who was on the call and what it was about", "follow-ups": "what was promised"}},
    "social": {"when": "chat, stories, catching up",
               "sections": {"highlights": "the memorable bits", "plans": "anything planned"}},
}


def templates():
    t = dict(TEMPLATES)
    p = config.CONFIG_DIR / "templates.json"
    try:
        if p.exists():
            t.update(json.loads(p.read_text()))
    except (OSError, ValueError) as e:
        log.warning("templates.json ignored: %s", e)
    return t


def _template_block():
    return "\n".join(f'- "{k}" (when: {v["when"]}): sections ' + ", ".join(f'"{s}" = {d}' for s, d in v["sections"].items())
                     for k, v in templates().items())


CARD_PROMPT = """A conversation in Fred's home office just ended. Write its summary card as ONE JSON object only:
{"context": "one of the template names below, or other",
 "sections": {"<section name of that template>": ["short items"]},
 "folder": "the main subject area as a 1-3 word folder name (a project, a person, House, Work, Money...). Reuse an EXISTING FOLDER only if this conversation is clearly about that same subject; otherwise name a new one, never a near-synonym of an existing folder (Household vs Home vs Family Logistics)",
 "title": "under 8 words", "summary": "2 to 4 sentences", "decisions": ["decisions actually made, with who"],
 "action_items": [{"owner": "", "task": "imperative, under 15 words", "specifics": "", "due_phrase": "", "due_date": null,
                   "line_ids": [ids]}],
 "people": ["names exactly as in the lines"]}
Only real commitments are action items; remarks addressed to Larry (the assistant) are not. line_ids must be the
#numbers shown. Pick the template whose "when" fits best and fill only its sections (empty lists are fine).
TEMPLATES:
{templates}
EXISTING FOLDERS: {folders}
Lines (#id [date time] speaker: text):
{lines}
"""


def _lines(db, c):
    """A conversation's room lines, by its time span (line ids are not always in time order)."""
    return [dict(r) for r in db.execute("""SELECT s.id, s.ts, s.dur, s.text, s.speaker_id, COALESCE(sp.name,'Unknown') AS speaker
        FROM segments s LEFT JOIN speakers sp ON sp.id=s.speaker_id
        WHERE s.ts>=? AND s.ts<=? AND s.noise=0 AND COALESCE(s.media,0) NOT IN (1,2) AND s.source IS NULL
        ORDER BY s.ts, s.id""", (c["ts0"] - 0.01, c["ts1"]))]


def _fmt(l, with_date=False):
    from datetime import datetime
    return f"#{l['id']} [{datetime.fromtimestamp(l['ts']).strftime('%Y-%m-%d %H:%M' if with_date else '%H:%M')}] {l['speaker']}: {l['text']}"


def _ask(prompt, keys, model):
    text, gen, used, secs, note = llm.generate(prompt, mode=config.NIGHTLY_GEN, claude_model=model, timeout=config.NIGHTLY_TIMEOUT)
    obj = storyboard.parse_json(text)
    if any(k not in obj for k in keys):
        raise ValueError("missing keys")
    return obj, f"{gen}:{used}", secs


def update(db, now=None, summarize=True):
    """One pass from the ideas worker: grow/close conversations, refresh the live summary, write cards."""
    now = now or time.time()
    out = {"opened": [], "closed": [], "live": False}
    hw = int(dbm.get_meta(db, "conv_scan_id", "0"))
    # room lines only (uploads keep their own recording time), once the media tag has settled (or a minute passed)
    rows = db.execute("""SELECT id, ts, dur, speaker_id FROM segments WHERE id>? AND noise=0 AND COALESCE(media,0) NOT IN (1,2)
                         AND source IS NULL AND (media IS NOT NULL OR ts < ?) AND ts+dur < ? ORDER BY ts, id""",
                      (hw, now - 60, now - 5)).fetchall()
    for r in rows:
        cur = db.execute("SELECT * FROM conversations WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
        if cur and (r["ts"] - cur["ts1"] > CONV_GAP or _turned_over(db, cur, r)):
            _close(db, cur["id"])
            out["closed"].append(cur["id"])
            cur = None
        if not cur:
            cur_ins = db.execute("""INSERT OR IGNORE INTO conversations(key, ts0, ts1, first_seg, last_seg, lines, status, created)
                                    VALUES(?,?,?,?,?,1,'open',?)""", (f"conv:{r['id']}", r["ts"], r["ts"] + r["dur"], r["id"], r["id"], now))
            if cur_ins.rowcount:
                out["opened"].append(cur_ins.lastrowid)
        else:
            db.execute("UPDATE conversations SET ts1=MAX(ts1, ?), last_seg=?, lines=lines+1 WHERE id=?", (r["ts"] + r["dur"], r["id"], cur["id"]))
        hw = max(hw, r["id"])
        dbm.set_meta(db, "conv_scan_id", hw)          # the highest id seen (rows come in time order, not id order)
    cur = db.execute("SELECT * FROM conversations WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
    if cur and now - cur["ts1"] > CONV_GAP:
        _close(db, cur["id"])
        out["closed"].append(cur["id"])
        cur = None
    if summarize:
        for cid in [r[0] for r in db.execute("SELECT id FROM conversations WHERE status='closed' AND card IS NULL AND card_tries<3 ORDER BY id LIMIT 2")]:
            try:
                card(db, cid)
            except Exception as e:
                db.execute("UPDATE conversations SET card_tries=card_tries+1, card_error=? WHERE id=?", (str(e)[:200], cid))
                log.warning("card for conversation %d failed: %s", cid, e)
        if cur:
            out["live"] = live(db, cur, now)
    return out


def _turned_over(db, cur, r):
    """True when the voices of the last TURNOVER seconds are all gone and someone else is talking."""
    if not r["speaker_id"]:
        return False
    recent = {x[0] for x in db.execute("""SELECT DISTINCT speaker_id FROM segments WHERE ts>=? AND ts<=? AND speaker_id IS NOT NULL
                                          AND noise=0 AND COALESCE(media,0) NOT IN (1,2) AND source IS NULL""",
                                       (max(cur["ts0"], cur["ts1"] - TURNOVER), cur["ts1"]))}
    return bool(recent) and r["speaker_id"] not in recent and r["ts"] - cur["ts1"] > 120


def _close(db, cid):
    c = db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
    people = sorted({l["speaker"] for l in _lines(db, c)})
    db.execute("UPDATE conversations SET status='closed', closed_at=?, people=? WHERE id=?", (time.time(), json.dumps(people), cid))
    if c["lines"] < MIN_LINES:
        db.execute("UPDATE conversations SET card='{}', card_tries=0 WHERE id=?", (cid,))   # too short for a card
    notify.send({"t": "conv", "id": cid})


def live(db, cur, now):
    """Refresh the rolling summary of the open conversation when enough new lines came in (rate limited)."""
    st = json.loads(dbm.get_meta(db, "live_summary", "null") or "null") or {}
    if st.get("conv_id") == cur["id"] and (now - st.get("updated", 0) < LIVE_EVERY or cur["lines"] - st.get("lines", 0) < LIVE_MIN_NEW):
        return False
    if cur["lines"] < 2:
        return False
    lines = _lines(db, cur)[-120:]
    obj, model, secs = _ask(LIVE_PROMPT.replace("{lines}", "\n".join(_fmt(l) for l in lines)), ["headline", "bullets"], config.NIGHTLY_CLAUDE_MODEL)
    people = sorted({l["speaker"] for l in lines})
    st = {"conv_id": cur["id"], "headline": obj.get("headline", ""), "bullets": obj.get("bullets", [])[:6], "open": obj.get("open", [])[:4],
          "people": people, "since": cur["ts0"], "lines": cur["lines"], "updated": now, "model": model, "seconds": secs}
    dbm.set_meta(db, "live_summary", json.dumps(st))
    notify.send({"t": "live"})
    return True


def card(db, cid):
    from . import nightly
    c = db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
    lines = _lines(db, c)
    if len(lines) < MIN_LINES:
        db.execute("UPDATE conversations SET card='{}' WHERE id=?", (cid,))
        return None
    text = "\n".join(_fmt(l, True) for l in lines)[:40000]
    from . import folders
    obj, model, secs = _ask(CARD_PROMPT.replace("{templates}", _template_block()).replace("{folders}", ", ".join(folders.names(db)) or "(none yet)")
                            .replace("{lines}", text), ["title", "summary"], config.NIGHTLY_CLAUDE_MODEL)
    if obj.get("folder"):
        folders.place(db, "conversation", cid, obj["folder"])        # B3-11
    tpl = templates().get(obj.get("context"))
    if tpl:     # keep only that template's sections, in its order
        obj["sections"] = {k: list(obj.get("sections", {}).get(k, []))[:8] for k in tpl["sections"]}
    else:
        obj["context"], obj["sections"] = "other", {}
    from datetime import datetime
    day = datetime.fromtimestamp(c["ts0"]).strftime("%Y-%m-%d")
    new_items = nightly.store_items(db, day, obj.get("action_items"), {l["id"] for l in lines}, {l["id"]: l["ts"] for l in lines}, model)
    obj["action_item_ids"] = new_items
    db.execute("UPDATE conversations SET card=?, model=?, card_error=NULL WHERE id=?", (json.dumps(obj), model, cid))
    from . import events, plugins
    events.emit(db, "conversation.card", {"conversation_id": cid, "day": day, "title": obj.get("title"), "summary": obj.get("summary"),
                                          "decisions": obj.get("decisions", []), "people": obj.get("people", []), "context": obj.get("context")})
    try:
        plugins.tick(db, event="conversation.card")          # plugins scheduled 'on conversation.card'
    except Exception:
        log.exception("conversation.card plugins failed")
    try:
        from . import drafts
        drafts.maybe_auto(db, cid)                             # B3-13: follow-up drafts, never sent
    except Exception:
        log.exception("follow-up drafts failed")
    notify.send({"t": "conv", "id": cid})
    log.info("conversation %d card: %s (%d new action items)", cid, obj.get("title"), len(new_items))
    return obj


def cards_for_day(db, day):
    from datetime import datetime, timedelta
    a = datetime.strptime(day, "%Y-%m-%d").timestamp()
    b = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).timestamp()
    out = []
    for r in db.execute("SELECT * FROM conversations WHERE ts0>=? AND ts0<? ORDER BY ts0", (a, b)):
        c = json.loads(r["card"]) if r["card"] else None
        out.append({"id": r["id"], "ts0": r["ts0"], "ts1": r["ts1"], "lines": r["lines"], "status": r["status"],
                    "people": json.loads(r["people"]) if r["people"] else [], "first_seg": r["first_seg"],
                    "card": c if c else None, "pending": r["status"] == "closed" and r["card"] is None})
    return out
