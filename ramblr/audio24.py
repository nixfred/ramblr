"""Rolling 24 h raw-audio store (B3-15), approved by Fred 2026-10-24 2:45 and 2:49 AM ET.

The capture stream is written as compressed Opus (~12 kb/s mono, about 100 MB per 24 h of continuous audio) in
10-minute chunks under ~/.local/share/ramblr/audio-24h/<day>/<start-ms>.opus (dir mode 700, outside the repo,
never backed up or synced). Muted time is never written: muting closes the chunk. A size cap stops writing (it
never deletes). Deleting chunks older than 24 h is ramblr.retention's job, dry run first.

Each line on the site under 24 h old gets a play button: clip() cuts that line's span out of the chunk(s)."""
import logging
import os
import subprocess
import time
from datetime import datetime

from . import config

log = logging.getLogger("ramblr.audio24")
CHUNK_SECONDS = 600


def root():
    return config.AUDIO24_DIR


def _dir_bytes():
    total = 0
    for dp, _, files in os.walk(root()):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


def recover_parts():
    """A crash leaves the open chunk as *.opus.part, which retention (only *.opus) would never see: that audio would
    outlive the 24 h promise. Only one Writer (capture) ever writes .part files, so at its start every .part is an
    orphan: finalize it to .opus so the normal 24 h retention removes it on time."""
    if not root().exists():
        return 0
    n = 0
    for day in os.listdir(root()):
        dp = root() / day
        if not dp.is_dir():
            continue
        for f in os.listdir(dp):
            if f.endswith(".opus.part"):
                p = dp / f
                try:
                    os.chmod(p, 0o600)
                    p.rename(p.with_suffix(""))
                    n += 1
                except OSError:
                    log.exception("could not finalize %s", f)
    if n:
        log.warning("finalized %d interrupted audio chunk(s) so retention covers them", n)
    return n


class Writer:
    """Fed raw s16le 16 kHz mono bytes by the capture loop. One niced ffmpeg encoder per chunk."""

    def __init__(self):
        self.proc = None
        self.start = None        # epoch of the chunk's first sample
        self.samples = 0
        self.last_end = None
        self.full = False
        self.checked = 0.0
        recover_parts()


    def _open(self, start):
        day = datetime.fromtimestamp(start).strftime("%Y-%m-%d")
        d = root() / day
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(root(), 0o700)
        os.chmod(d, 0o700)
        path = d / f"{int(start * 1000)}.opus.part"
        self.proc = subprocess.Popen(["nice", "-n", "19", "ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(config.RATE), "-ac", "1",
                                      "-i", "pipe:0", "-c:a", "libopus", "-b:a", config.AUDIO24_BITRATE, "-application", "voip",
                                      "-compression_level", "2", "-flush_packets", "1", "-f", "ogg", str(path)],
                                     stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     preexec_fn=lambda: os.umask(0o077))   # files are private from the first byte
        self.path, self.start, self.samples = path, start, 0

    def close(self):
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.wait(10)
            except Exception:
                self.proc.kill()
            if self.path.exists():
                os.chmod(self.path, 0o600)
                self.path.rename(self.path.with_suffix(""))     # .opus.part -> .opus once complete
        self.proc = None

    def write(self, data, end_ts):
        if not config.AUDIO24_ENABLED:
            return
        n = len(data) // 2
        begin = end_ts - n / config.RATE
        now = time.monotonic()
        if now - self.checked > 60:
            self.checked = now
            self.full = _dir_bytes() > config.AUDIO24_CAP
            if self.full:
                log.error("audio-24h is over its %d MB cap: not writing (nothing is deleted here)", config.AUDIO24_CAP // 2**20)
        if self.full:
            self.close()
            return
        if self.proc and (begin - self.last_end > 1.0 or self.samples / config.RATE >= CHUNK_SECONDS):
            self.close()        # a gap (mute, mic switch) or a full chunk: start a new file at the true time
        if not self.proc:
            self._open(begin)
        try:
            self.proc.stdin.write(data)
            self.samples += n
            self.last_end = end_ts
        except (BrokenPipeError, OSError):
            log.error("audio-24h encoder died; reopening")
            self.proc = None


def chunks():
    """[(start_epoch, path)] for every finished chunk, oldest first."""
    out = []
    if not root().exists():
        return out
    for day in sorted(os.listdir(root())):
        dp = root() / day
        if not dp.is_dir():
            continue
        for f in os.listdir(dp):
            if f.endswith(".opus"):
                try:
                    out.append((int(f.split(".")[0]) / 1000, dp / f))
                except ValueError:
                    pass
    return sorted(out)


def clip(ts, dur, pad=0.3):
    """Opus/ogg bytes for [ts, ts+dur] or None. Lines older than 24 h never get audio."""
    if time.time() - ts > 86400:
        return None
    a, b = ts - pad, ts + dur + pad
    # The newest chunk that starts at or before the line. Chunks are often shorter than CHUNK_SECONDS (a mute,
    # mic switch or restart closes them early), so "first chunk within 600 s" picked an older, wrong file.
    before = [(start, path) for start, path in chunks() if start <= a]
    for start, path in before[-1:]:
        if a < start + CHUNK_SECONDS + 5:
            p = subprocess.run(["nice", "-n", "10", "ffmpeg", "-v", "error", "-ss", f"{max(0, a - start):.2f}", "-t", f"{b - a:.2f}",
                                "-i", str(path), "-c:a", "libopus", "-b:a", "24k", "-f", "ogg", "pipe:1"], capture_output=True, timeout=30)
            return p.stdout if p.returncode == 0 and p.stdout else None
    return None


def store_file(path, start):
    """Uploaded recordings join the same 24 h store (compressed), named by when they were recorded."""
    if not config.AUDIO24_ENABLED:
        return None
    day = datetime.fromtimestamp(start).strftime("%Y-%m-%d")
    d = root() / day
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(root(), 0o700)
    ms = int(start * 1000)
    while (d / f"{ms}.opus").exists() or (d / f"{ms}.opus.part").exists():
        ms += 1                                     # never overwrite another recording that started the same ms
    out = d / f"{ms}.opus"
    subprocess.run(["nice", "-n", "19", "ffmpeg", "-v", "error", "-n", "-i", str(path), "-ac", "1", "-ar", str(config.RATE), "-c:a", "libopus",
                    "-b:a", config.AUDIO24_BITRATE, "-application", "voip", "-f", "ogg", str(out)], timeout=1800, check=True)
    os.chmod(out, 0o600)
    return out
