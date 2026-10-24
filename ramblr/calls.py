"""Call mode (2026-10-24, a Zoom call): a caller's voice coming out of the speakers sounds like TV to the
classifier (YAMNet tv ~0.9, and a long call barely pauses, so speech occupancy is ~1), so room lines got hidden as
TV and the caller's voice never became a person. While a call is live, room-mic lines are never auto-tagged media
and the diarizer treats them as talk.

A call is live when
  - a call app (Zoom, Teams, Webex, Discord, Slack, Skype...) has an audio stream on this machine, or a browser or
    Electron app is capturing the microphone (WebRTC: Meet, Zoom web, Teams web), or
  - the call switch is on (the Call button on the site, or `python -m ramblr.ctl call on [minutes]`); it holds until
    switched off, or CALL_SWITCH_HOURS (4 h) as a safety net so a forgotten switch cannot hide the TV for days, or
  - a heartbeat arrives from the machine that has the call (`POST /call` on the router, tailnet only, own key; the
    sender for a Mac is clients/ramblr-call-heartbeat). Each heartbeat keeps call mode on for its TTL (max 180 s).
    2026-10-24: the call was on fnix and only its sound reached gus, so gus alone could not see it.
Capture records every live stretch in the `calls` table (never deleted), so later re-decisions see the same truth."""
import json
import logging
import subprocess
import time

from . import config, state

log = logging.getLogger("ramblr.calls")
CALL_APPS = ("zoom", "teams", "webex", "discord", "slack", "skype", "signal", "whatsapp", "jitsi", "facetime", "meet",
             "telegram", "element", "ringcentral", "gotomeeting", "bluejeans")
BROWSERS = ("brave", "chrome", "chromium", "firefox", "vivaldi", "edge", "opera", "electron", "webkit", "epiphany")
NOT_CALLS = ("ramblr", "quickshell", "peak detect", "cava", "pavucontrol", "pulsemeter", "easyeffects", "obs")
EXTEND = 30.0          # a call stretch continues if it was seen within this many seconds


def _streams(kind):
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", kind], capture_output=True, text=True, timeout=5).stdout
        return json.loads(out or "[]")
    except Exception:
        return []


def _who(o):
    p = o.get("properties") or {}
    return " ".join(str(p.get(k) or "") for k in ("application.name", "application.process.binary", "node.name",
                                                    "media.name", "application.id")).lower()


def detect(source_outputs=None, sink_inputs=None):
    """Name of the app holding a live call on this machine, or None. Arguments are for tests (pactl JSON lists)."""
    outs = _streams("source-outputs") if source_outputs is None else source_outputs
    ins = _streams("sink-inputs") if sink_inputs is None else sink_inputs
    for o in outs:                                   # something is listening to a mic
        w = _who(o)
        if o.get("corked") or any(n in w for n in NOT_CALLS):
            continue
        for app in CALL_APPS + BROWSERS:
            if app in w:
                return app
    for o in ins:                                    # a call app is playing audio
        w = _who(o)
        if not o.get("corked") and any(app in w for app in CALL_APPS):
            return next(app for app in CALL_APPS if app in w)
    return None


def switch(now=None):
    """The manual call switch from state.json: (on, until)."""
    st = state.load()
    until = float(st.get("call_until") or 0)
    return (until > (now or time.time())), until


def live(now=None):
    """(app or reason, how) if the switch or gus itself says a call is live now, else (None, None). The heartbeat
    signal writes its own rows (router), so it needs no polling here."""
    on, _ = switch(now)
    if on:
        return "call switch", "manual"
    app = detect()
    return (app, "auto") if app else (None, None)


def heartbeat(db, app, ttl, now=None):
    """A POST from the machine holding the call: call mode stays on for ttl seconds (capped)."""
    now = now or time.time()
    ttl = max(10.0, min(float(ttl or 90), config.CALL_TTL_MAX))
    app = (str(app or "call")[:40]).strip() or "call"
    return record(db, now, now + ttl, app, "heartbeat"), ttl


def status(db, now=None):
    """For the header indicator: which of the three signals say a call is live."""
    now = now or time.time()
    on, until = switch(now)
    hb = db.execute("SELECT app, t1 FROM calls WHERE how='heartbeat' AND t1>? ORDER BY t1 DESC LIMIT 1", (now,)).fetchone()
    from . import db as dbm
    local = dbm.get_meta(db, "capture_call", "") or ""
    local = "" if local == "call switch" else local
    sig = (["switch"] if on else []) + (["heartbeat"] if hb else []) + (["gus"] if local else [])
    return {"live": bool(sig), "signals": sig, "switch_until": round(until) if on else None,
            "heartbeat_app": hb["app"] if hb else None, "heartbeat_until": round(hb["t1"]) if hb else None, "gus_app": local or None}


def record(db, t0, t1, app, how):
    """Merge into a stretch of the same kind that overlaps [t0, t1] (within EXTEND s), in both directions, else start a
    new one. 2026-10-24 13:03: a retroactive record of 12:29-13:03 met capture's first 13:03 tick and only moved the
    end, so the earlier half of the call was never recorded."""
    cur = db.execute("SELECT id FROM calls WHERE how=? AND t1>=? AND t0<=? ORDER BY t1 DESC LIMIT 1", (how, t0 - EXTEND, t1 + EXTEND)).fetchone()
    if cur:
        db.execute("UPDATE calls SET t0=MIN(t0, ?), t1=MAX(t1, ?) WHERE id=?", (t0, t1, cur["id"]))
        return cur["id"]
    return db.execute("INSERT INTO calls(t0, t1, app, how) VALUES(?,?,?,?)", (t0, t1, app, how)).lastrowid


def during(db, a, b):
    """True if a call was live at any time in [a, b] (room mic)."""
    return db.execute("SELECT 1 FROM calls WHERE t0<? AND t1>? LIMIT 1", (b, a)).fetchone() is not None
