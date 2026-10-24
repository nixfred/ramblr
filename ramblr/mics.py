"""Mic selection. Lists PipeWire/Pulse input sources (monitors excluded) and decides which one capture records.
Never touches the system default source: Voxtype depends on it. Ramblr only ever names a source for its own
parec stream (parec -d <node>)."""
import json
import subprocess
import time

from . import config

_cache = {"t": 0.0, "list": None}


def is_monitor(s):
    return (s.get("name", "").endswith(".monitor") or s.get("monitor_source")
            or (s.get("properties") or {}).get("device.class") == "monitor")


def list_sources(max_age=0.0):
    """[{name, description, state, available, default}] for real inputs. max_age reuses a recent listing."""
    if _cache["list"] is not None and time.monotonic() - _cache["t"] <= max_age:
        return _cache["list"]
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True, text=True, timeout=5).stdout
        raw = json.loads(out or "[]")
        default = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        return _cache["list"] or []
    mics = []
    for s in raw:
        if is_monitor(s):
            continue
        ports = s.get("ports") or []
        # a source whose every port reports "not available" is plugged-in hardware with nothing on the jack
        avail = not ports or any(p.get("availability") != "not available" for p in ports)
        mics.append({"name": s["name"], "description": s.get("description") or s["name"], "state": s.get("state", ""),
                     "available": bool(avail), "default": s["name"] == default})
    _cache.update(t=time.monotonic(), list=mics)
    return mics


def home_source(mics):
    """The fallback mic: RAMBLR_SOURCE from ~/.config/ramblr/config.env (the BRIO), else the system default."""
    names = {m["name"] for m in mics}
    if config.SOURCE in names:
        return config.SOURCE
    d = next((m["name"] for m in mics if m["default"]), None)
    return d or "@DEFAULT_SOURCE@"


def pick(chosen, mics):
    """(source to record, warning or ''). Falls back to the home mic while the chosen one is missing; the
    caller re-picks every few seconds, so capture returns to the chosen mic by itself when it reappears."""
    names = {m["name"] for m in mics if m["available"]}
    home = home_source(mics)
    if not chosen or chosen == home:
        return home, ("" if home in names or home.startswith("@") else "home mic missing, recording the system default")
    if chosen in names:
        return chosen, ""
    label = next((m["description"] for m in mics if m["name"] == chosen), chosen)
    return home, f"chosen mic '{label}' is not connected; recording the fallback mic until it returns"
