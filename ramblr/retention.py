"""24 h raw-audio retention (B3-15). Scope, exactly as Fred approved (2026-10-24 2:45 + 2:49 AM ET): ONLY raw audio
chunk files (*.opus) inside ~/.local/share/ramblr/audio-24h whose audio ended more than 24 h ago. Transcripts,
voiceprints, the DB and everything else are never touched.

Delete is OFF until verified: every run writes a log line per candidate to retention-log.jsonl. While delete is
off the run is a dry run. `python -m ramblr.retention verify` checks the newest dry run: if it listed at least one
file and every listed file is a regular *.opus inside audio-24h, more than 24 h old, deletes are switched on
(meta retention_delete=1). Each delete re-checks the same rules for that file just before it is removed.

Auto-verify (Fred, 2026-10-24 6:55 AM ET: "Let Larry auto-verify the dry run and turn on deletes"): the CLI
`verify` also ARMS auto-verify (meta retention_armed). While armed and deletes are still off, the hourly tick
verifies the newest dry run before each pass, so deletes switch on after the first clean dry run that lists real
candidates. Nothing is armed by code alone; only the CLI verify (Fred's go) arms it.

  python -m ramblr.retention            one pass (dry run until verified)
  python -m ramblr.retention verify     arm auto-verify, check the newest dry-run log, enable deletes if it is clean
  python -m ramblr.retention status
"""
import json
import os
import sys
import time

from . import audio24, config, db as dbm

LOG = config.DATA / "retention-log.jsonl"
MAX_AGE = 86400


def in_scope(path, start, now):
    """True only for a finished raw-audio chunk inside the store whose audio ended over 24 h ago."""
    try:
        real = os.path.realpath(path)
        rootp = os.path.realpath(audio24.root())
        ok_place = real.startswith(rootp + os.sep) and not os.path.islink(path) and os.path.isfile(path)
        end = start + audio24.CHUNK_SECONDS
        return ok_place and str(path).endswith(".opus") and now - end > MAX_AGE and now - os.path.getmtime(path) > MAX_AGE
    except OSError:
        return False


def run(db, now=None):
    now = now or time.time()
    enabled = dbm.get_meta(db, "retention_delete", "0") == "1"
    run_id = int(now)
    out = {"run": run_id, "mode": "delete" if enabled else "dry-run", "candidates": 0, "deleted": 0, "bytes": 0, "refused": 0}
    with open(LOG, "a") as f:
        for start, path in audio24.chunks():
            if now - (start + audio24.CHUNK_SECONDS) <= MAX_AGE:
                continue
            ok = in_scope(path, start, now)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            rec = {"run": run_id, "mode": out["mode"], "path": str(path), "bytes": size,
                   "age_h": round((now - start - audio24.CHUNK_SECONDS) / 3600, 2), "in_scope": ok}
            out["candidates"] += 1
            if not ok:
                out["refused"] += 1
                rec["action"] = "refused"
            elif enabled:
                os.remove(path)            # the one approved delete: a raw-audio chunk older than 24 h
                out["deleted"] += 1
                out["bytes"] += size
                rec["action"] = "deleted"
            else:
                rec["action"] = "would delete"
            f.write(json.dumps(rec) + "\n")
    os.chmod(LOG, 0o600)
    dbm.set_meta(db, "retention_last", json.dumps(out))
    return out


def tick(db, now=None):
    """The hourly timer: if deletes are still off and auto-verify is armed, verify the newest dry run first
    (deletes turn on only after the log shows only raw audio files older than 24 h), then run a pass."""
    v = None
    if dbm.get_meta(db, "retention_delete", "0") != "1" and dbm.get_meta(db, "retention_armed", "0") == "1":
        v = verify(db)
    return {"verify": v, "pass": run(db, now)}


def arm(db, by="cli"):
    if dbm.get_meta(db, "retention_armed", "0") != "1":
        dbm.set_meta(db, "retention_armed", "1")
        dbm.set_meta(db, "retention_armed_at", json.dumps({"at": time.time(), "by": by}))


def verify(db, arm_auto=False):
    """Turn deletes on only after a clean dry run that actually listed old audio files. arm_auto (the CLI) also
    arms the hourly auto-verify so a not-yet-verifiable state turns on by itself later."""
    if arm_auto:
        arm(db)
    armed = dbm.get_meta(db, "retention_armed", "0") == "1"
    if not LOG.exists() or not LOG.stat().st_size:
        return {"enabled": False, "armed": armed, "why": "no dry run has listed any audio yet (nothing older than 24 h)", "files": []}
    rows = [json.loads(l) for l in open(LOG) if l.strip()]
    dry = [r for r in rows if r["mode"] == "dry-run"]
    if not dry:
        return {"enabled": False, "armed": armed, "why": "no dry-run entries", "files": []}
    last = max(r["run"] for r in dry)
    batch = [r for r in dry if r["run"] == last]
    rootp = os.path.realpath(audio24.root())
    bad = [r for r in batch if not (r["in_scope"] and r["path"].endswith(".opus") and os.path.realpath(r["path"]).startswith(rootp + os.sep)
                                    and r["age_h"] > 24)]
    files = [{"path": r["path"], "age_h": r["age_h"], "bytes": r["bytes"]} for r in batch]
    if bad or not batch:
        return {"enabled": False, "armed": armed, "why": f"{len(bad)} entries out of scope" if bad else "empty dry run",
                "bad": bad[:5], "files": files}
    dbm.set_meta(db, "retention_delete", "1")
    dbm.set_meta(db, "retention_verified", json.dumps({"run": last, "files": len(batch), "at": time.time(), "list": files}))
    return {"enabled": True, "armed": armed, "verified_run": last, "files": files}


def status(db):
    ch = audio24.chunks()
    return {"delete_enabled": dbm.get_meta(db, "retention_delete", "0") == "1",
            "auto_verify_armed": dbm.get_meta(db, "retention_armed", "0") == "1",
            "verified": json.loads(dbm.get_meta(db, "retention_verified", "null") or "null"),
            "last_run": json.loads(dbm.get_meta(db, "retention_last", "null") or "null"),
            "chunks": len(ch), "oldest": ch[0][0] if ch else None,
            "bytes": sum(os.path.getsize(p) for _, p in ch if os.path.exists(p)), "cap": config.AUDIO24_CAP}


if __name__ == "__main__":
    db = dbm.connect(config.DB_PATH)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    print(json.dumps({"run": run, "tick": tick, "verify": lambda d: verify(d, arm_auto=True), "status": status}.get(cmd, lambda d: {"error": "usage: run|verify|status"})(db), indent=1))
