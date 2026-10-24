"""Music ID for the media log (B2-2): Chromaprint fingerprint -> AcoustID lookup.

Only the fingerprint (a compact hash of the sound) and the duration ever leave gus, never audio.

- Fingerprinting is live: chromaprint (fpcalc) was installed on gus 2026-10-24 06:58 with Fred's yes. While music
  plays, the capture service fingerprints ~20 s from RAM and stores only the hash (media_tracks, status pending).
- Song naming (the AcoustID lookup) is a stub until RAMBLR_MUSIC_LOOKUP=1 and a working RAMBLR_ACOUSTID_KEY are in
  config.env / secrets.env (never printed). Fred, 6:55 AM: the key in secrets.env answers "invalid API key", keep it
  stubbed while he checks it.
- A rejected key never spoils fingerprints: they stay pending, lookups pause for an hour, the status says why.
Music sessions are logged from the classifier either way, just without a song title."""
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
import soundfile as sf

from . import config

API = "https://api.acoustid.org/v2/lookup"
REJECT_PAUSE = 3600
_rejected = {"at": 0.0, "why": ""}


class KeyRejected(RuntimeError):
    """AcoustID refused the client key: not the track's fault, so the track stays pending."""


def fpcalc():
    return shutil.which("fpcalc")


def status():
    if not fpcalc():
        return "blocked: chromaprint (fpcalc) not installed"
    if not config.MUSIC_LOOKUP:
        return "stub: song naming off (RAMBLR_MUSIC_LOOKUP=0); fingerprints are kept for later"
    if not config.ACOUSTID_KEY:
        return "stub: no AcoustID key (fingerprints are kept and looked up once the key exists)"
    if time.time() - _rejected["at"] < REJECT_PAUSE:
        return f"stub: AcoustID rejected the key ({_rejected['why']}); fingerprints stay pending"
    return "ready"


def fingerprint(samples):
    """(fingerprint, seconds) from float32 16 kHz mono, or None. The clip lives in the tmpfs spool for the
    second fpcalc needs it and is removed at once."""
    exe = fpcalc()
    if not exe or len(samples) < config.RATE * 8:
        return None
    config.SPOOL.mkdir(parents=True, exist_ok=True)
    path = config.SPOOL / f"fp-{os.getpid()}-{time.monotonic_ns()}.wav"
    try:
        sf.write(path, np.asarray(samples, np.float32), config.RATE, subtype="PCM_16")
        p = subprocess.run(["nice", "-n", "19", exe, "-json", str(path)], capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            return None
        d = json.loads(p.stdout)
        return d["fingerprint"], float(d["duration"])
    finally:
        path.unlink(missing_ok=True)


def lookup(fp, duration, timeout=15):
    """AcoustID lookup: sends client key, duration and fingerprint only. Returns {title, artist, acoustid, score}."""
    if not config.ACOUSTID_KEY:
        raise RuntimeError("no AcoustID key")
    body = urllib.parse.urlencode({"client": config.ACOUSTID_KEY, "duration": int(duration), "fingerprint": fp,
                                   "meta": "recordings"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(API, data=body), timeout=timeout) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read() or b"{}").get("error") or {}
        except ValueError:
            err = {}
        if err.get("code") in (4, 5, 6) or "api key" in str(err.get("message", "")).lower():   # invalid/unknown client key
            raise KeyRejected(str(err.get("message") or "invalid API key")[:80])
        raise
    best = max(d.get("results", []), key=lambda x: x.get("score", 0), default=None)
    if not best:
        return {"title": "", "artist": "", "acoustid": "", "score": 0}
    rec = (best.get("recordings") or [{}])[0]
    return {"title": rec.get("title", ""), "artist": ", ".join(a["name"] for a in rec.get("artists", [])),
            "acoustid": best.get("id", ""), "score": best.get("score", 0)}


def lookup_pending(db, limit=5):
    """Called from the ideas worker loop: resolve stored fingerprints once a key exists."""
    if status() != "ready":
        return 0
    n = 0
    for r in db.execute("SELECT id, fingerprint, duration FROM media_tracks WHERE status='pending' ORDER BY id LIMIT ?", (limit,)).fetchall():
        try:
            m = lookup(r["fingerprint"], r["duration"])
            db.execute("UPDATE media_tracks SET status=?, title=?, artist=?, acoustid=?, score=? WHERE id=?",
                       ("found" if m["title"] else "unknown", m["title"], m["artist"], m["acoustid"], m["score"], r["id"]))
            n += 1
        except KeyRejected as e:                       # the key, not the track: keep it pending, pause lookups
            _rejected.update(at=time.time(), why=str(e))
            break
        except Exception as e:
            db.execute("UPDATE media_tracks SET status='error', error=? WHERE id=?", (type(e).__name__, r["id"]))
    return n
