"""iPhone ramble capture (B3-0): an uploaded recording goes through the same pipeline as the room mic (Silero VAD,
Whisper on the NPU, diarization from per-line tmpfs clips), lands on the same timeline tagged source 'iPhone',
and becomes an idea card that builds its storyboard after the usual quiet period.

Audio rule: the uploaded file waits in the tmpfs spool and is removed as soon as it is transcribed (or kept in
the 24 h store once B3-15 is live). The text is kept forever."""
import json
import logging
import os
import secrets
import subprocess
import time
from datetime import datetime

import numpy as np

from . import config, db as dbm, ideas, notify

log = logging.getLogger("ramblr.uploads")
INBOX = config.SPOOL / "uploads"


def ensure_secret(var, comment):
    """Create <var> in ~/.config/ramblr/secrets.env (chmod 600) if it is missing. Never printed: Fred reads it from
    that file. Returns the value for this process only."""
    path = config.CONFIG_DIR / "secrets.env"
    text = path.read_text() if path.exists() else ""
    for line in text.splitlines():
        if line.startswith(var + "=") and line.split("=", 1)[1].strip():
            return line.split("=", 1)[1].strip()
    key = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(("" if text.endswith("\n") or not text else "\n") + f"# {comment}\n" + f"{var}={key}\n")
    os.chmod(path, 0o600)
    return key


def ensure_key():
    """RAMBLR_UPLOAD_KEY, created if missing (see ensure_secret)."""
    if not config.UPLOAD_KEY:
        config.UPLOAD_KEY = ensure_secret("RAMBLR_UPLOAD_KEY", "iPhone upload key (B3-0): the Shortcut sends it as 'Authorization: Bearer <key>'")
    return config.UPLOAD_KEY


def probe(path):
    """(duration seconds, recorded-at epoch or None) from the file's own metadata."""
    p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", str(path)], capture_output=True, text=True, timeout=30)
    fmt = json.loads(p.stdout or "{}").get("format", {})
    dur = float(fmt.get("duration") or 0)
    ct = (fmt.get("tags") or {}).get("creation_time")
    rec = None
    if ct:
        try:
            rec = datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
        except ValueError:
            rec = None
    return dur, rec


def decode(path):
    """float32 mono 16 kHz samples via ffmpeg (already installed), niced."""
    p = subprocess.run(["nice", "-n", "10", "ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(config.RATE), "pipe:1"],
                       capture_output=True, timeout=1800)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg could not decode the upload: " + p.stderr.decode(errors="replace")[-200:])
    return np.frombuffer(p.stdout, dtype=np.float32)


class Transcriber:
    """One NPU Whisper + one ingest pipeline for the ideas worker, created on the first upload and reused after
    (its ASR thread and DB connection live as long as the worker; about 250 MB)."""

    def __init__(self):
        self.ing, self.used = None, 0.0

    def get(self, source):
        from .asr import NpuWhisper
        from .pipeline import Ingest
        if self.ing is None:
            self.ing = Ingest(config.DB_PATH, NpuWhisper(), spool=True, media=False, source=source)
        self.ing.source = source
        self.ing.ids = []
        self.used = time.monotonic()
        return self.ing


MAX_ATTEMPTS = 3


def process(db, uid, tr):
    """Transcribe one upload. Done only if no line was lost. A failure before any line was stored keeps the file
    and retries (3 attempts, within 24 h); a failure after some lines were stored is 'partial' and is not retried,
    because a retry would store those lines twice."""
    u = db.execute("SELECT * FROM uploads WHERE id=?", (uid,)).fetchone()
    if not u or u["status"] not in ("queued", "working"):
        return None
    path = INBOX / u["file"]
    db.execute("UPDATE uploads SET status='working', attempts=attempts+1 WHERE id=?", (uid,))
    attempt = (u["attempts"] or 0) + 1
    ids, keep = [], False
    try:
        dur, rec = probe(path)
        start = u["recorded_at"] or rec or (u["created"] - dur)
        audio = decode(path)
        ing = tr.get(u["source"])
        failed0 = ing.failed
        step = config.RATE // 10
        for i in range(0, len(audio), step):
            ing.feed(audio[i:i + step], now=start + (i + step) / config.RATE)
        ing.flush(now=start + len(audio) / config.RATE)
        ing.wait_idle()
        ids = list(ing.ids)
        lost = ing.failed - failed0
        if lost and not ids:
            raise RuntimeError(f"all {lost} utterances failed to transcribe")
        iid = None
        talk = [r["id"] for r in db.execute(f"SELECT id FROM segments WHERE id IN ({','.join('?' * len(ids))}) AND noise=0", ids)] if ids else []
        if talk:
            iid, _ = ideas.create(db, talk, "iphone", key=f"upload:{uid}")
            db.execute("UPDATE ideas SET open=0, last_line_ts=? WHERE id=?", (time.time(), iid))   # builds after the quiet period
            notify.send({"t": "idea", "id": iid})
        db.execute("UPDATE uploads SET status=?, error=?, duration=?, recorded_at=?, segments=?, idea_id=?, finished=? WHERE id=?",
                   ("partial" if lost else "done", f"{lost} utterances failed to transcribe" if lost else None,
                    round(dur, 1), start, len(ids), iid, time.time(), uid))
        if ids:
            notify.send({"t": "upd", "ids": ids})
        from . import events
        events.emit(db, "upload.done", {"upload_id": uid, "lines": len(ids), "idea_id": iid, "seconds": round(dur, 1), "lost": lost})
        log.info("upload %d: %.0fs -> %d lines (%d lost), idea %s", uid, dur, len(ids), lost, iid)
        return {"lines": len(ids), "idea_id": iid, "lost": lost}
    except Exception as e:
        log.exception("upload %d failed (attempt %d)", uid, attempt)
        keep = (not ids and attempt < MAX_ATTEMPTS and path.exists() and time.time() - u["created"] < 86400)
        db.execute("UPDATE uploads SET status=?, error=?, finished=? WHERE id=?",
                   ("queued" if keep else ("partial" if ids else "error"), f"attempt {attempt}: {str(e)[:280]}", time.time(), uid))
        return None
    finally:
        if not keep:
            try:
                if path.exists():
                    from . import audio24
                    rec_start = db.execute("SELECT recorded_at FROM uploads WHERE id=?", (uid,)).fetchone()[0]
                    if rec_start and time.time() - rec_start < 86400:
                        audio24.store_file(path, rec_start)   # B3-15: uploads join the 24 h compressed store
            except Exception:
                log.exception("upload %d: could not add to the 24 h store", uid)
            path.unlink(missing_ok=True)      # the uploaded original is never kept past its last attempt


def pending(db):
    return [r[0] for r in db.execute("SELECT id FROM uploads WHERE status IN ('queued','working') ORDER BY id")]
