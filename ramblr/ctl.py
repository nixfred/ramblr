"""Command line used by the Hyprland hotkeys and the bar widget.

  python -m ramblr.ctl mute toggle|on|off      stop/start recording (state.json; capture follows within ~1 s)
  python -m ramblr.ctl call on [MIN]|off|status call mode for a call on another device (holds until off; safety net 4 h)
  python -m ramblr.ctl status                  one JSON line: listening/muted, mic, warning
  python -m ramblr.ctl watch [--interval N]    a status line every N s (the bar widget reads this)
"""
import json
import subprocess
import sys
import time

from . import config, db as dbm, notify, state


def toast(text):
    # a desktop notification only: no window, no focus change
    subprocess.run(["notify-send", "-a", "Ramblr", "-u", "low", "-t", "1800", "Ramblr", text],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)


def status(db):
    st = state.load()
    hb = dbm.get_meta(db, "capture_heartbeat")
    age = None if hb is None else round(time.time() - float(hb), 1)
    mode = dbm.get_meta(db, "capture_state", "starting")
    if age is None or age > 45:
        mode = "down"
    elif st.get("muted"):
        mode = "muted" if mode == "muted" else "muting"
    return {"mode": mode, "muted": bool(st.get("muted")), "source": dbm.get_meta(db, "capture_source", ""),
            "chosen": st.get("source") or "", "warning": dbm.get_meta(db, "capture_warning", ""), "heartbeat_age": age,
            "today_lines": db.execute("SELECT COUNT(*) FROM segments WHERE day=date('now','localtime') AND noise=0").fetchone()[0]}


def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd == "mute":
        arg = argv[1] if len(argv) > 1 else "toggle"
        cur = state.load().get("muted")
        new = (not cur) if arg == "toggle" else arg == "on"
        state.update(by="hotkey", muted=new)
        notify.send({"t": "cap"})
        toast("Muted: nothing is being recorded" if new else "Listening")
        print(json.dumps({"muted": new}))
    elif cmd == "call":
        from . import calls
        arg = argv[1] if len(argv) > 1 else "status"
        if arg in ("on", "off"):
            mins = float(argv[2]) if arg == "on" and len(argv) > 2 else config.CALL_SWITCH_HOURS * 60
            state.update(by="ctl", call_until=time.time() + mins * 60 if arg == "on" else 0)
            notify.send({"t": "cap"})
            toast(f"Call mode on for {mins:g} min: call voices are people, not TV" if arg == "on" else "Call mode off")
        on, until = calls.switch()
        print(json.dumps({"call_switch": on, "until": round(until) if on else None, "detected": calls.detect()}))
    elif cmd == "status":
        print(json.dumps(status(dbm.connect(config.DB_PATH))))
    elif cmd == "watch":
        iv = float(argv[argv.index("--interval") + 1]) if "--interval" in argv else 2
        db = dbm.connect(config.DB_PATH)
        while True:
            print(json.dumps(status(db)), flush=True)
            time.sleep(max(1, iv))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
