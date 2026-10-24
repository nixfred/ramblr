"""Conversation actions as DRAFTS only (B3-13). Nothing here sends anything, anywhere.

After a conversation card with action items is written (or when Fred presses "Draft follow-ups"), the cheap model
drafts up to three follow-ups: an email, a message, a calendar event proposal, a task list or a note. Drafts live
only in Ramblr's DB. Fred edits, copies, approves (a status meaning "I will send this myself") or dismisses them.
There is no send path: no mail, no messages, no calendar writes, no Gmail drafts (Law 18 keeps Fred's Gmail
drafts his own doing). Drafts are never deleted."""
import json
import time

from . import config, llm, storyboard

KINDS = ("email", "message", "event", "tasks", "note", "x-post")

XPROMPT = """Turn what Fred said into X (Twitter) post DRAFTS he might post himself. Return ONE JSON object only:
{"drafts": [{"body": "the post text", "why": "what it is based on"}]}
Rules: 1 to 3 drafts; plain facts in Fred's direct voice, no hype, no pomp, no emoji walls, no hashtags spam;
keep his own words where they are good; never invent numbers, names or claims; tag a person's @handle at most once
and only if he said it; length is free (no 280 cap) but tight beats long. These are drafts: nothing is posted.
SOURCE:
{src}
"""


def make_x_post(db, idea_id=None, segment_ids=None):
    """Rant -> X post drafts (B4-10). Stored as drafts (kind x-post); Ramblr never posts anything."""
    from datetime import datetime
    if idea_id:
        rows = db.execute("""SELECT s.ts, s.text, COALESCE(sp.name,'Unknown') AS speaker FROM idea_lines il JOIN segments s ON s.id=il.segment_id
                             LEFT JOIN speakers sp ON sp.id=s.speaker_id WHERE il.idea_id=? ORDER BY s.ts""", (idea_id,)).fetchall()
    elif segment_ids:
        ph = ",".join("?" * len(segment_ids))
        rows = db.execute(f"""SELECT s.ts, s.text, COALESCE(sp.name,'Unknown') AS speaker FROM segments s LEFT JOIN speakers sp ON sp.id=s.speaker_id
                              WHERE s.id IN ({ph}) AND COALESCE(s.media,0) NOT IN (1,2) ORDER BY s.ts""", [int(x) for x in segment_ids]).fetchall()
    else:
        raise ValueError("give an idea or lines")
    if not rows:
        raise LookupError("no lines")
    src = "\n".join(f"[{datetime.fromtimestamp(r['ts']).strftime('%H:%M')}] {r['speaker']}: {r['text']}" for r in rows)[-12000:]
    text, gen, model, secs, note = llm.generate(XPROMPT.replace("{src}", src), mode=config.NIGHTLY_GEN, claude_model=config.ASK_CLAUDE_MODEL,
                                                timeout=config.NIGHTLY_TIMEOUT)
    made = 0
    for d in (storyboard.parse_json(text).get("drafts") or [])[:3]:
        body = str(d.get("body") or "").strip()
        if body:
            db.execute("""INSERT INTO drafts(conversation_id, kind, recipient, subject, body, why, status, model, created, updated)
                          VALUES(NULL, 'x-post', 'X', ?, ?, ?, 'draft', ?, ?, ?)""",
                       (f"from idea {idea_id}" if idea_id else "from selected lines", body[:4000], str(d.get("why") or "")[:300],
                        f"{gen}:{model}", time.time(), time.time()))
            made += 1
    return {"made": made}

PROMPT = """A conversation in Fred's home office ended. Draft the follow-ups Fred would most likely want to send or
keep, at most 3, as ONE JSON object only:
{"drafts": [{"kind": "email|message|event|tasks|note", "to": "who it is for, by name or role ('' if unknown)",
             "subject": "short", "body": "the full draft text, ready to paste, in Fred's plain direct voice",
             "why": "which action item or decision it follows"}]}
Rules: only follow-ups the conversation clearly calls for; never invent addresses, phone numbers or facts; an
"event" body states title, day/time words exactly as said, and who; "tasks" is a short checklist. If nothing calls
for a follow-up, return {"drafts": []}.
CARD: {card}
LINES (#id [date time] speaker: text):
{lines}
"""


def make_for_conversation(db, cid, force=False):
    from .conversations import _lines, _fmt
    c = db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
    if not c or not c["card"] or c["card"] == "{}":
        raise LookupError("no card for that conversation")
    if not force and db.execute("SELECT 1 FROM drafts WHERE conversation_id=?", (cid,)).fetchone():
        return {"made": 0, "note": "drafts already exist"}
    card = json.loads(c["card"])
    lines = _lines(db, c)
    text, gen, model, secs, note = llm.generate(PROMPT.replace("{card}", json.dumps({k: card.get(k) for k in ("title", "summary", "decisions", "action_items")}))
                                                .replace("{lines}", "\n".join(_fmt(l, True) for l in lines)[-30000:]),
                                                mode=config.NIGHTLY_GEN, claude_model=config.NIGHTLY_CLAUDE_MODEL, timeout=config.NIGHTLY_TIMEOUT)
    made = 0
    for d in (storyboard.parse_json(text).get("drafts") or [])[:3]:
        kind = d.get("kind") if d.get("kind") in KINDS else "note"
        body = str(d.get("body") or "").strip()
        if not body:
            continue
        db.execute("""INSERT INTO drafts(conversation_id, kind, recipient, subject, body, why, status, model, created, updated)
                      VALUES(?,?,?,?,?,?,'draft',?,?,?)""", (cid, kind, str(d.get("to") or "")[:120], str(d.get("subject") or "")[:200],
                                                           body[:8000], str(d.get("why") or "")[:300], f"{gen}:{model}", time.time(), time.time()))
        made += 1
    return {"made": made}


def maybe_auto(db, cid):
    """Called after a card is written: draft follow-ups only when the card has action items."""
    c = db.execute("SELECT card FROM conversations WHERE id=?", (cid,)).fetchone()
    card = json.loads(c["card"]) if c and c["card"] else {}
    if card.get("action_items"):
        return make_for_conversation(db, cid)
    return {"made": 0}


def update(db, did, body=None, status=None, who="site"):
    if status and status not in ("draft", "approved", "dismissed"):
        raise ValueError("status must be draft, approved or dismissed")
    r = db.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()
    if not r:
        raise LookupError("no such draft")
    if body is not None and body != r["body"]:
        db.execute("INSERT INTO draft_edits(draft_id, old, new, who, created) VALUES(?,?,?,?,?)", (did, r["body"], body[:8000], who, time.time()))
        db.execute("UPDATE drafts SET body=?, updated=? WHERE id=?", (body[:8000], time.time(), did))
    if status:
        db.execute("UPDATE drafts SET status=?, updated=? WHERE id=?", (status, time.time(), did))
    return {}
