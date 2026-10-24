"""Runtime switches in ~/.config/ramblr/state.json: mute and the chosen mic. Written by the site, the hotkeys
(`python -m ramblr.ctl`) and the bar widget; the capture service re-reads it within a second of any change."""
import fcntl
import json
import os
import time

from . import config

DEFAULTS = {"muted": False, "source": None, "changed": 0, "by": ""}


def load():
    try:
        with open(config.STATE_FILE) as f:
            return {**DEFAULTS, **json.load(f)}
    except (FileNotFoundError, ValueError):
        return dict(DEFAULTS)


def update(by="", **changes):
    """Atomic read-modify-write under a lock file, so two writers never lose each other's change."""
    config.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = str(config.STATE_FILE) + ".lock"
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        st = load()
        st.update(changes)
        st["changed"], st["by"] = time.time(), by[:80]
        tmp = str(config.STATE_FILE) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, config.STATE_FILE)
    return st


def mtime():
    try:
        return config.STATE_FILE.stat().st_mtime_ns
    except FileNotFoundError:
        return 0
