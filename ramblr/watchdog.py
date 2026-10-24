"""Capture watchdog (outage 2026-10-24: Whisper raised on every line and nothing was stored for minutes unnoticed).

Runs from ramblr-watchdog.timer every 2 minutes, reading the DB only. It acts when the room mic is listening and
  - the capture heartbeat is stale (the loop is stuck), or
  - the media classifier heard speech for a while (WATCHDOG_MINUTES) but not one room line was stored.
Then, once per incident: mark the configured Whisper model as given up on (capture starts on whisper-base.en),
restart ramblr-capture and alert Fred. If it is still broken after that one restart it only alerts, never restarts
again until a line has landed. `python -m ramblr.watchdog clear` lets the configured model run again.
In-process the ASR does the same for errors (asr.py): rebuild once, then fall back once."""
import json
import logging
import os
import subprocess
import sys
import time

from . import config, db as dbm

log = logging.getLogger("ramblr.watchdog")
SPEECH_MIN = 0.5          # classifier speech score that counts as someone talking
SPEECH_WINDOWS = 10       # ~30 s of speech windows (2.88 s each) inside the watch span
HEARTBEAT_STALE = 180     # capture publishes every 15 s
COOLDOWN = 3600           # never two watchdog restarts within an hour, even across incidents


def alert(title, message):
    """Tell Fred through larry-notify (never blocks, never raises). Also an ERROR line in the journal."""
    log.error("ALERT %s: %s", title, message)
    if os.environ.get("RAMBLR_NO_ALERT") == "1" or "PYTEST_CURRENT_TEST" in os.environ:
        return
    try:
        payload = json.dumps({"hook_event_name": "Notification", "title": title, "message": message,
                              "source": "ramblr", "cwd": str(config.HOME / "Projects/ramblr")})
        subprocess.run(["larry-notify"], input=payload.encode(), timeout=10, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        log.exception("larry-notify failed")


def check(db, now=None):
    """None if healthy (or not listening), else a short reason."""
    now = now or time.time()
    span = config.WATCHDOG_MINUTES * 60
    meta = lambda k, d=None: dbm.get_meta(db, k, d)
    if meta("capture_state") != "listening":
        return None
    hb = float(meta("capture_heartbeat", 0) or 0)
    if now - hb > HEARTBEAT_STALE:
        return f"capture heartbeat is {int(now - hb)} s old"
    started = float(meta("capture_started", 0) or 0)
    if now - started < span:
        return None                                   # too soon after a (re)start to judge
    speech = db.execute("SELECT COUNT(*) FROM media_windows WHERE source IS NULL AND ts1>? AND speech>=?",
                        (now - span, SPEECH_MIN)).fetchone()[0]
    if speech < SPEECH_WINDOWS:
        return None
    lines = db.execute("SELECT COUNT(*) FROM segments WHERE source IS NULL AND ts+dur>?", (now - span - 60,)).fetchone()[0]
    if lines:
        return None
    return f"speech heard in {speech} classifier windows over {config.WATCHDOG_MINUTES:g} min but no room line was stored"


def _state():
    try:
        return json.loads(config.WATCHDOG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save(st):
    config.WATCHDOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.WATCHDOG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, config.WATCHDOG_FILE)


def tick(db, now=None, restart=None):
    """One watchdog pass. Returns the action taken: ok | recovered | restarted | alerted | waiting."""
    from .asr import mark_fallback
    now = now or time.time()
    restart = restart or (lambda: subprocess.run(["systemctl", "--user", "restart", "ramblr-capture.service"],
                                                 timeout=120, check=False))
    st = _state()
    if st.get("restarted_at"):
        landed = db.execute("SELECT COUNT(*) FROM segments WHERE source IS NULL AND ts>?", (st["restarted_at"],)).fetchone()[0]
        if landed:
            alert("Ramblr recovered", f"{landed} room line(s) stored since the watchdog restart ({st.get('reason', '')})")
            _save({"recovered_at": now, "last_reason": st.get("reason"), "last_restart": st["restarted_at"]})
            return "recovered"
    why = check(db, now)
    if not why:
        return "ok"
    if st.get("restarted_at"):
        if not st.get("alerted_again"):
            st["alerted_again"] = now
            _save(st)
            alert("Ramblr still not transcribing", f"{why}. Already restarted once at "
                  f"{time.strftime('%H:%M', time.localtime(st['restarted_at']))}; not restarting again.")
            return "alerted"
        return "waiting"
    if now - float(st.get("last_restart", 0)) < COOLDOWN:
        if not st.get("alerted_cooldown"):
            st["alerted_cooldown"] = now
            _save(st)
            alert("Ramblr not transcribing", f"{why}. The watchdog restarted capture less than an hour ago; not again.")
            return "alerted"
        return "waiting"
    model = dbm.get_meta(db, "asr_model", "") or ""
    if model and config.WHISPER_FALLBACK.name != model:
        mark_fallback(config.WHISPER_DIR.name, f"watchdog: {why}")
    _save({"restarted_at": now, "reason": why})
    alert("Ramblr watchdog restarting capture", f"{why}. Restarting ramblr-capture once"
          + (f" on {config.WHISPER_FALLBACK.name}" if model != config.WHISPER_FALLBACK.name else "") + ".")
    restart()
    return "restarted"


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stdout)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    if cmd == "clear":
        config.ASR_FALLBACK_FILE.unlink(missing_ok=True)       # only the marker file: lets the configured model run
        print("fallback marker cleared; restart ramblr-capture to load", config.WHISPER_DIR.name)
        return
    if cmd == "status":
        from .asr import fallback_marker
        db = dbm.connect(config.DB_PATH)
        print(json.dumps({"check": check(db), "state": _state(), "fallback": fallback_marker()}, indent=1))
        return
    db = dbm.connect(config.DB_PATH)
    print(tick(db))


if __name__ == "__main__":
    main()
