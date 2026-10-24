"""Voice routing (B3-14, approved in batch 3): "Larry, tell <bot> to <task>" is sent to Larry Manager's webhook
routine "Ramblr voice command router". Ramblr only routes; it never acts on a command itself.

- Detected live by ramblr-ideas, after a line's media tag has settled: TV/music lines never trigger a command.
- Waits for the diarizer (or 30 s) so the speaker label and its confidence travel with the command.
- command_id = hash of the line + target + task: the same command is never routed twice.
- The router is Ramblr's own voice router on gus (ramblr/router.py, Fred 7:01 AM); its URL and generated key are in
  ~/.config/ramblr/secrets.env (RAMBLR_VOICE_ROUTER_URL / RAMBLR_VOICE_ROUTER_KEY). Without them every command
  is logged as "stub: no router". The router's verdict is stored: held (needs Fred's yes, approve on the
  Commands page), ready / forwarded (read-only asks), approved, upstream-error.
  Neither is ever printed or logged. Three tries with back-off, every attempt recorded; rows are never deleted.

Payload: {"command_id", "target", "task", "transcript", "timestamp", "speaker", "speaker_confidence", "source"}."""
import hashlib
import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime

from . import config, db as dbm, notify

log = logging.getLogger("ramblr.commands")
ENABLED = True
PHRASE = re.compile(r"\blarry\b[\s,.!-]{0,4}(?:please\s+)?tell\s+(?P<target>[a-z0-9][\w .'-]{0,40}?)\s+to\s+(?P<task>.+)", re.I)   # the original form
# F (Fred, 2026-10-24): natural phrasing too. "Larry, tell/ask/have/get X (to) Y", "tell Larry to have X do Y", and,
# when the wake word was clipped (vic seg 10 "Have the finance bot look up ..."), a line that starts with the verb or
# with "<name> bot to ..." - but only if the target clearly names a bot, so "have the kids clean up" never routes.
WAKE_RX = re.compile(r"\blarry\b[\s,.!?-]{0,4}(?:please\s+)?", re.I)
VERB_RX = re.compile(r"^(?:please\s+)?(?:tell|ask|have|get)\s+", re.I)
TELL_LARRY_RX = re.compile(r"\btell\s+larry\s+to\s+", re.I)
TO_RX = re.compile(r"^(?:the\s+)?(?P<target>[a-z0-9][\w .'-]{0,40}?)\s+to\s+(?P<task>.+)$", re.I)
BOTISH_RX = re.compile(r"^(?:the\s+)?(?P<target>(?:[\w'-]+\s+){0,3}?[\w'-]*bots?)\b[,:.;!]?\s+(?:to\s+)?(?P<task>.+)$", re.I)
VOCATIVE_RX = re.compile(r"^(?:hey\s+|ok(?:ay)?\s+)?(?P<target>(?:[\w'-]+\s+){0,2}?[\w'-]*bots?)[,:!]\s+(?P<task>.+)$", re.I)
BOT_ALIASES = {"finance": "Finances", "finances": "Finances",     # spoken name (no "the", no "bot") -> bot name
               "grok": "Grok Bot", "grock": "Grok Bot", "groc": "Grok Bot"}
BOT_ALIASES.update({k.strip().lower(): v.strip() for k, v in
                    (x.split("=", 1) for x in os.environ.get("RAMBLR_BOT_ALIASES", "").split(",") if "=" in x)})
# Whisper writes host names by sound ("Set it up on gas"): after on/to/at/from/onto they map to the real host
HOST_ALIASES = {"gas": "gus", "guss": "gus", "gust": "gus", "goose": "gus", "vick": "vic", "vik": "vic", "f nix": "fnix"}
HOST_ALIASES.update({k.strip().lower(): v.strip() for k, v in
                     (x.split("=", 1) for x in os.environ.get("RAMBLR_HOST_ALIASES", "").split(",") if "=" in x)})
HOST_RX = re.compile(r"\b(on|to|at|from|onto)\s+(" + "|".join(sorted(map(re.escape, HOST_ALIASES), key=len, reverse=True)) + r")\b", re.I)
JOIN_GAP = float(os.environ.get("RAMBLR_COMMAND_JOIN_GAP", 4.0))   # s between lines of one spoken command (11:50 test: 1.2-4.3 s)
JOIN_MAX = 8
NOT_BOTS = {"robot", "robots", "abbot", "abbots", "sabot", "turbot", "bot's"}
MAX_TRIES = 3


def router():
    return os.environ.get("RAMBLR_VOICE_ROUTER_URL", ""), os.environ.get("RAMBLR_VOICE_ROUTER_KEY", "")


def _bot_key(target):
    t = re.sub(r"^the\s+", "", target.strip().lower())
    return re.sub(r"\s*bots?$", "", t).strip()            # "the finance bot", "Grockbot" (one word) -> finance, grock


def canonical(target):
    """'the finance bot' -> 'Finances' via BOT_ALIASES; otherwise the spoken name without a leading 'the'."""
    return BOT_ALIASES.get(_bot_key(target)) or re.sub(r"^the\s+", "", target.strip(), flags=re.I)


def _botlike(target):
    t = target.strip().lower()
    if t.split()[-1:] and t.split()[-1] in NOT_BOTS:
        return False
    return bool(re.search(r"[a-z]bots?$|\sbots?$", t)) or t in ("bot", "bots") or _bot_key(t) in BOT_ALIASES


def hosts(task):
    """'set it up on gas' -> 'set it up on gus' (only right after on/to/at/from/onto)."""
    return HOST_RX.sub(lambda m: f"{m.group(1)} {HOST_ALIASES[m.group(2).lower()]}", task)


def starts_command(text):
    """Could a command begin in this line? (wake word, a leading verb, 'tell Larry to', or a bot named by name)"""
    t = " ".join((text or "").split())
    return bool(WAKE_RX.search(t) or VERB_RX.match(t) or TELL_LARRY_RX.search(t)
                or any(_botlike(w) for w in re.findall(r"[\w'-]+", t[:80])))


def _split(rest, woke):
    """(target, task) from what follows the verb, or None."""
    rest = " ".join(rest.split()).strip()
    for rx in (TO_RX, BOTISH_RX):
        m = rx.match(rest)
        if not m:
            continue
        target = " ".join(m.group("target").split()).strip(" ,.")
        task = " ".join(m.group("task").split()).strip()
        if not target or len(task) < 2 or len(target.split()) > 5:
            continue
        if rx is TO_RX and not (woke or _botlike(target)):
            continue
        if rx is BOTISH_RX and not _botlike(target):
            continue
        return canonical(target), task
    return None


def parse(text):
    p = _parse(text)
    return (p[0], hosts(p[1])) if p else None


def _parse(text):
    m = PHRASE.search(text)
    if m:
        target = " ".join(m.group("target").split()).strip(" ,.")
        task = " ".join(m.group("task").split()).strip()
        if target and len(task) >= 2:
            return canonical(target), task
    t = " ".join((text or "").split())
    m = TELL_LARRY_RX.search(t)                     # "tell Larry to have X do Y"
    if m:
        rest = t[m.end():]
        v = VERB_RX.match(rest)
        got = _split(rest[v.end():], True) if v else None
        if got:
            return got
    m = WAKE_RX.search(t)                           # "Larry, ask/have/get/tell X (to) Y"
    if m:
        rest = t[m.end():]
        v = VERB_RX.match(rest)
        if v:
            got = _split(rest[v.end():], True)
            if got:
                return got
    v = VERB_RX.match(t)                            # clipped wake word: "Have the finance bot look up ..."
    if v:
        got = _split(t[v.end():], False)
        if got:
            return got
    m = BOTISH_RX.match(t)                          # clipped verb too: "random bot to continue working on ..."
    if m and _botlike(m.group("target")) and re.search(r"bots?\s+to\s+", t[:60], re.I):
        return canonical(" ".join(m.group("target").split())), " ".join(m.group("task").split())
    m = VOCATIVE_RX.match(t)                        # no wake word, addressed by name: "Grockbot, go look at ..."
    if m and _botlike(m.group("target")) and len(m.group("task").split()) >= 2:
        return canonical(" ".join(m.group("target").split())), " ".join(m.group("task").split())
    return None


def _run(db, r, now, settle, diar_wait):
    """r plus the lines that follow it from the same voice, each within JOIN_GAP s of the previous one: one spoken
    command often lands as several lines (11:50 test: 5 lines, gaps 1.2-4.3 s). Returns (rows, ready); not ready
    while the next line could still be arriving (ASR) or its voice is not known yet."""
    rows = [r]
    while len(rows) < JOIN_MAX:
        last = rows[-1]
        nxt = db.execute("""SELECT id, ts, dur, text, media, noise, diar, sim, manual, source, speaker_id FROM segments
                            WHERE source IS ? AND id != ? AND ts > ? AND ts <= ? ORDER BY ts LIMIT 1""",
                         (r["source"], last["id"], last["ts"], last["ts"] + last["dur"] + JOIN_GAP)).fetchone()
        if not nxt or nxt["noise"] or nxt["media"] in (1, 2):
            break
        if nxt["diar"] == 0 and now - (nxt["ts"] + nxt["dur"]) < diar_wait:
            return rows, False
        a = next((x["speaker_id"] for x in rows if x["speaker_id"] is not None), None)
        if a is not None and nxt["speaker_id"] is not None and nxt["speaker_id"] != a:
            break                                   # someone else spoke: the command ended
        rows.append(nxt)
    last = rows[-1]
    return rows, now - (last["ts"] + last["dur"]) >= settle


def detect(db, now=None, settle=8.0, diar_wait=30.0):
    """Route-ready command rows for new lines. Returns new command ids."""
    if not ENABLED:
        return []
    now = now or time.time()
    hw = int(dbm.get_meta(db, "command_scan_id", "0"))
    new = []
    rows = db.execute("""SELECT s.id, s.ts, s.dur, s.text, s.media, s.noise, s.diar, s.sim, s.manual, s.source, s.speaker_id,
                                COALESCE(sp.name, 'Unknown') AS speaker, COALESCE(sp.named, 0) AS named
                         FROM segments s LEFT JOIN speakers sp ON sp.id=s.speaker_id
                         WHERE s.id>? AND s.ts+s.dur<? ORDER BY s.id""", (hw, now - settle)).fetchall()
    consumed = 0
    for r in rows:
        if r["id"] <= consumed:
            dbm.set_meta(db, "command_scan_id", r["id"])
            continue
        if r["diar"] == 0 and now - (r["ts"] + r["dur"]) < diar_wait:
            break            # wait for the voice id (keeps order; the next pass picks it up)
        if r["noise"] or r["media"] in (1, 2) or r["source"] in ("Zoom", "Wispr") or not starts_command(r["text"]):
            dbm.set_meta(db, "command_scan_id", r["id"])
            continue         # the TV saying "Larry, tell...", or an imported transcript, never routes
        run, ready = _run(db, r, now, settle, diar_wait)
        joined = " ".join(" ".join((x["text"] or "").split()) for x in run)
        p = parse(joined) or (parse(r["text"]) if len(run) > 1 else None)
        if not p:
            dbm.set_meta(db, "command_scan_id", r["id"])
            continue
        if not ready:
            break            # the command may still be going on: next pass
        voice = next((x for x in run if x["speaker_id"] is not None), None)
        who = dict(r) if voice is None else {**dict(r), **{k: voice[k] for k in ("speaker_id", "diar", "sim", "manual")}}
        if voice is not None and voice["id"] != r["id"]:
            sp = db.execute("SELECT name, named FROM speakers WHERE id=?", (voice["speaker_id"],)).fetchone()
            who["speaker"], who["named"] = (sp["name"], sp["named"]) if sp else ("Unknown", 0)
        from . import me
        g = me.gate(db, who, now)
        if g == "wait":                            # voice still unknown: never treat that as Fred; retry next pass
            break
        dbm.set_meta(db, "command_scan_id", run[-1]["id"])
        consumed = run[-1]["id"]                   # the rest of the command is not a command of its own
        if g == "no" and who["named"]:             # B4-5: someone else we know by name said it: not Fred's command
            db.execute("""INSERT OR IGNORE INTO command_intents(segment_id, ts, phrase, intent, status, created, command_id, target, task,
                          speaker, result, tries) VALUES(?,?,?,'tell','ignored',?,?,?,?,?,'not Fred''s voice',0)""",
                       (r["id"], r["ts"], joined[:500], time.time(), hashlib.sha1(f"{r['id']}|ignored".encode()).hexdigest()[:16],
                        p[0][:60], p[1][:500], who["speaker"]))
            continue
        target, task = p
        cid = hashlib.sha1(f"{r['id']}|{target.lower()}|{task.lower()}".encode()).hexdigest()[:16]
        conf = 1.0 if who["manual"] else (round(float(who["sim"]), 3) if who["sim"] is not None else None)
        held = g == "no"                           # F: an unidentified voice fails CLOSED: kept, held for Fred's yes
        cur = db.execute("""INSERT OR IGNORE INTO command_intents(segment_id, ts, phrase, intent, status, created, command_id, target, task,
                            speaker, confidence, tries, result, voice_hold) VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
                         (r["id"], r["ts"], joined[:500], "tell", "held" if held else "queued", time.time(), cid, target[:60], task[:500],
                          who["speaker"] if who["named"] else (who["speaker"] + " (unnamed voice)"), conf,
                          "needs Fred's yes: voice not confirmed as Fred" if held else None, 1 if held else 0))
        if cur.rowcount:
            new.append(cid)
            notify.send({"t": "cmd", "id": cid})
    return new


def payload(row):
    return {"command_id": row["command_id"], "target": row["target"], "task": row["task"], "transcript": row["phrase"],
            "timestamp": datetime.fromtimestamp(row["ts"]).astimezone().isoformat(timespec="seconds"),
            "speaker": row["speaker"], "speaker_confidence": row["confidence"], "source": "ramblr",
            "fred_approved": bool(row["fred_approved"]) if "fred_approved" in row.keys() else False}


ROUTER_STATES = {"held", "ready", "forwarded", "approved", "upstream-error"}


def verdict_status(v):
    """The gus voice router's answer -> (command status, result line for the Commands page)."""
    st = v["status"] if v["status"] in ROUTER_STATES else "sent"
    if st == "held":
        return st, f"held: needs Fred's yes ({v.get('why') or 'could change something'})"
    return st, (f"{v.get('class', '')}: {v.get('result') or st}").strip(": ")[:400]


def approve(db, command_id):
    """Fred's yes for a held command (Commands page). Only the router can release it; returns the new status."""
    url, key = router()
    r = db.execute("SELECT * FROM command_intents WHERE command_id=?", (command_id,)).fetchone()
    if not r:
        raise LookupError("unknown command")
    if r["status"] != "held":
        return r["status"]
    if r["voice_hold"] and not r["sent_at"] and not r["tries"]:
        # held here by the voice gate (never sent): Fred's yes queues it for the router, marked as already approved
        db.execute("UPDATE command_intents SET status='queued', fred_approved=1, result='Fred said yes' WHERE id=?", (r["id"],))
        notify.send({"t": "cmd", "id": command_id})
        return "queued"
    if not (url and key):
        raise RuntimeError("no router configured")
    base = url.rsplit("/route", 1)[0]
    req = urllib.request.Request(f"{base}/approve/{command_id}", data=b"", method="POST", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        v = json.loads(resp.read(2000))
    st, res = verdict_status(v)
    db.execute("UPDATE command_intents SET status=?, result=? WHERE id=?", (st, res, r["id"]))
    notify.send({"t": "cmd", "id": command_id})
    return st


def route(db, now=None):
    """Send queued commands to the router (or mark them stubbed)."""
    now = now or time.time()
    url, key = router()
    done = 0
    for r in db.execute("SELECT * FROM command_intents WHERE status IN ('queued','retry') AND COALESCE(next_try,0)<=? ORDER BY id LIMIT 20",
                        (now,)).fetchall():
        if r["command_id"] is None:
            continue
        if not (url and key):
            db.execute("UPDATE command_intents SET status='stub', result='stub: no router configured (RAMBLR_VOICE_ROUTER_URL/KEY)' WHERE id=?", (r["id"],))
            notify.send({"t": "cmd", "id": r["command_id"]})
            continue
        body = json.dumps(payload(r)).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {key}", "Idempotency-Key": r["command_id"]})
        verdict = None
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                ok, res = 200 <= resp.status < 300, f"http {resp.status}"
                try:
                    raw = resp.read(2000).decode(errors="replace")
                    res = raw[:400] or res
                    verdict = json.loads(raw)
                except Exception:
                    pass
        except Exception as e:
            ok, res = False, type(e).__name__        # never the URL or key
        tries = (r["tries"] or 0) + 1
        status = "sent" if ok else ("failed" if tries >= MAX_TRIES else "retry")
        if ok and isinstance(verdict, dict) and verdict.get("status"):
            status, res = verdict_status(verdict)
        db.execute("UPDATE command_intents SET status=?, result=?, tries=?, sent_at=?, next_try=? WHERE id=?",
                   (status, res, tries, now if ok else None, now + 30 * (4 ** tries), r["id"]))
        notify.send({"t": "cmd", "id": r["command_id"]})
        done += 1
    return done
