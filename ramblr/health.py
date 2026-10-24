"""Health and storage dashboard data (B2-5): mic, NPU, database, disk, services. Read-only: nothing here acts."""
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta

from . import config, db as dbm, state

NPU = "/sys/class/accel/accel0/device"
UNITS = ["ramblr-capture", "ramblr-diarize", "ramblr-web", "ramblr-ideas", "ramblr-nightly.timer"]
_npu_last = {"t": None, "busy": None}


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def npu():
    """Busy % since the previous call (the driver's cumulative busy-time counter), frequency, memory in use."""
    if _npu_last["t"] is None and _read(f"{NPU}/npu_busy_time_us") is not None:   # first call: take a 0.5 s baseline
        _npu_last.update(t=time.monotonic(), busy=int(_read(f"{NPU}/npu_busy_time_us")))
        time.sleep(0.5)
    busy = _read(f"{NPU}/npu_busy_time_us")
    now = time.monotonic()
    pct = None
    if busy is not None:
        busy = int(busy)
        if _npu_last["t"] is not None and now > _npu_last["t"]:
            pct = round(100 * (busy - _npu_last["busy"]) / 1e6 / (now - _npu_last["t"]), 2)
        _npu_last.update(t=now, busy=busy)
    mem = _read(f"{NPU}/npu_memory_utilization")
    return {"present": busy is not None, "busy_pct": pct, "busy_total_s": round(busy / 1e6, 1) if busy is not None else None,
            "freq_mhz": _read(f"{NPU}/npu_current_frequency_mhz"), "max_mhz": _read(f"{NPU}/npu_max_frequency_mhz"),
            "memory_bytes": int(mem) if mem and mem.isdigit() else None}


def _proc(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            st = f.read().split(")")[-1].split()
        rss = int(_read(f"/proc/{pid}/statm").split()[1]) * os.sysconf("SC_PAGE_SIZE")
        cpu_s = (int(st[11]) + int(st[12])) / os.sysconf("SC_CLK_TCK")
        return {"rss_mb": round(rss / 2**20, 1), "cpu_s": round(cpu_s, 1)}
    except (OSError, IndexError, ValueError, AttributeError):
        return {}


def services():
    out = []
    for u in UNITS:
        props = subprocess.run(["systemctl", "--user", "show", u, "-p", "ActiveState,SubState,MainPID,ActiveEnterTimestamp,NRestarts,"
                                "NextElapseUSecRealtime,LastTriggerUSec"], capture_output=True, text=True, timeout=5).stdout
        p = dict(l.split("=", 1) for l in props.splitlines() if "=" in l)
        row = {"unit": u, "active": p.get("ActiveState"), "sub": p.get("SubState"), "since": p.get("ActiveEnterTimestamp") or None,
               "restarts": int(p.get("NRestarts") or 0) if (p.get("NRestarts") or "").isdigit() else None}
        if u.endswith(".timer"):
            row["next"] = p.get("NextElapseUSecRealtime") or None
            row["last"] = p.get("LastTriggerUSec") or None
        pid = int(p.get("MainPID") or 0)
        if pid:
            row.update(_proc(pid))
        out.append(row)
    return out


def storage(db):
    files = {}
    for suf in ("", "-wal", "-shm"):
        p = str(config.DB_PATH) + suf
        files["db" + suf] = os.path.getsize(p) if os.path.exists(p) else 0
    du = shutil.disk_usage(config.DATA)
    since = (datetime.now() - timedelta(days=13)).strftime("%Y-%m-%d")
    per_day = {r[0]: {"lines": r[1], "chars": r[2]} for r in db.execute(
        "SELECT day, COUNT(*), SUM(LENGTH(text)) FROM segments WHERE day>=? GROUP BY day", (since,))}
    days = [(datetime.now() - timedelta(days=13 - i)).strftime("%Y-%m-%d") for i in range(14)]
    growth = [{"day": d, "lines": per_day.get(d, {}).get("lines", 0)} for d in days]
    # real growth, not an estimate: one DB-size snapshot per day (main file + WAL), kept for 90 days
    log = json.loads(dbm.get_meta(db, "db_size_log", "{}") or "{}")
    today = datetime.now().strftime("%Y-%m-%d")
    log[today] = files["db"] + files["db-wal"]
    log = dict(sorted(log.items())[-90:])
    dbm.set_meta(db, "db_size_log", json.dumps(log))
    old = [d for d in log if d <= (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")]
    base_day = old[-1] if old else min(log)
    span_days = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(base_day, "%Y-%m-%d")).days
    week = round((log[today] - log[base_day]) / span_days * 7) if span_days >= 1 else None
    counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
              ("segments", "speakers", "embeddings", "ideas", "action_items", "summaries", "chunks", "media_windows", "media_plays")}
    return {"files": files, "db_total": sum(files.values()), "disk_free": du.free, "disk_total": du.total,
            "growth": growth, "week_bytes": week, "growth_basis": f"DB size {base_day} to {today}" if week is not None else "measuring: needs snapshots on 2 different days",
            "years_of_disk_at_this_rate": round(du.free / (week * 52), 1) if week and week > 0 else None,
            "counts": counts, "spool_bytes": sum(f.stat().st_size for f in config.SPOOL.iterdir()) if config.SPOOL.exists() else 0}


def mic(db):
    st = state.load()
    hb = dbm.get_meta(db, "capture_heartbeat")
    return {"state": dbm.get_meta(db, "capture_state"), "muted": bool(st.get("muted")), "source": dbm.get_meta(db, "capture_source"),
            "chosen": st.get("source"), "warning": dbm.get_meta(db, "capture_warning", ""),
            "heartbeat_age": round(time.time() - float(hb), 1) if hb else None,
            "level_rms": float(dbm.get_meta(db, "mic_rms", "0") or 0), "level_at": float(dbm.get_meta(db, "mic_rms_at", "0") or 0),
            "last_line": db.execute("SELECT MAX(ts) FROM segments").fetchone()[0],
            "asr": f"{dbm.get_meta(db, 'asr_model')} on {dbm.get_meta(db, 'asr_device')}",
            "media_classifier": dbm.get_meta(db, "media_device")}


def report(db):
    last = json.loads(dbm.get_meta(db, "nightly_last", "null") or "null")
    backup = json.loads(dbm.get_meta(db, "backup_last", "null") or "null")
    return {"at": time.time(), "mic": mic(db), "npu": npu(), "storage": storage(db), "services": services(),
            "nightly": {"started": last.get("started") if last else None,
                        "ok": (all("error" not in d for d in last.get("days", [])) and "error" not in (last.get("backup") or {})) if last else None},
            "backup": backup}
