"""VAD segmenter + ASR + storage. Shared by the live capture service and the offline test."""
import logging
import os
import itertools
import queue
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

from . import config, db as dbm, notify

log = logging.getLogger("ramblr.pipeline")


def make_vad():
    import sherpa_onnx as so
    cfg = so.VadModelConfig()
    cfg.silero_vad.model = str(config.VAD_MODEL)
    cfg.silero_vad.threshold = config.VAD_THRESHOLD
    cfg.silero_vad.min_silence_duration = config.VAD_MIN_SILENCE
    cfg.silero_vad.min_speech_duration = config.VAD_MIN_SPEECH
    cfg.silero_vad.max_speech_duration = config.VAD_MAX_SPEECH
    cfg.sample_rate = config.RATE
    cfg.num_threads = 1
    return so.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)


def spool_bytes():
    try:
        return sum(p.stat().st_size for p in config.SPOOL.iterdir())
    except FileNotFoundError:
        return 0


class Ingest:
    """feed() PCM in, speech segments are transcribed on the NPU and stored. Audio is never kept long term:
    the only copy is a tmpfs spool file that the diarizer deletes as soon as it has the voice embedding."""

    def __init__(self, db_path, asr, spool=True, media=True, source=None):
        self.db_path = db_path
        self.db = dbm.connect(db_path)          # main thread (heartbeat, reads); the ASR worker opens its own
        self.asr = asr
        self.vad = make_vad()
        self.fed = 0
        # pre-roll: Silero marks speech a little after it starts (12-112 ms measured), enough to shave a short first
        # word ("Good morning" -> "morning"). A ring of recent audio lets every segment get up to VAD_PREROLL s of
        # the sound before its onset, never reaching back into the previous segment.
        self._ring = np.zeros(int((config.VAD_MAX_SPEECH + config.VAD_LOOKBACK + 7) * config.RATE), dtype=np.float32)
        self._pre = int(config.VAD_PREROLL * config.RATE)
        self._last_end = 0
        # second look (2026-10-24 11:50): over music the running VAD dropped "Hey Larry, have Grockbot" entirely and
        # started the next line late ("go look at" -> "get"); which way it fell depended on the 512-sample window
        # phase. A fresh VAD over the not-yet-emitted audio before each onset (two phases) finds it again.
        self._look = config.VAD_LOOKBACK > 0
        self.rescued = 0
        self.spool = spool
        self.q = queue.Queue()
        self.stored = 0
        self.failed = 0              # segments lost after retries (uploads refuse to call themselves done if > 0)
        self.ids = []
        self.family = False          # B4-14: set by capture from state.json; lines spoken while on go to family.db
        self._n = itertools.count(1)
        self.seen = 0
        self.busy_ms = 0
        self._last_spool_check = 0.0
        self._spool_full = False
        self._last_alert = -1e9
        if spool:
            config.SPOOL.mkdir(parents=True, exist_ok=True)
            os.chmod(config.SPOOL, 0o700)
        self.worker = threading.Thread(target=self._work, daemon=True, name="asr")
        self.worker.start()
        self.media = None
        self.source = source
        if spool and media and config.MEDIA_ENABLED:      # live capture only; offline tests and uploads do not classify
            from .media import Worker
            self.media = Worker(db_path, source=source)   # each mic gets its own windows

    def feed(self, samples, now=None):
        """samples: float32 mono 16 kHz. `now` is the wall time of the end of this chunk."""
        now = now if now is not None else time.time()
        self._remember(samples)
        self.vad.accept_waveform(samples)
        self.fed += len(samples)
        if self.media:
            self.media.feed(samples, now)
        self._drain(now)

    def flush(self, now=None):
        now = now if now is not None else time.time()
        self.vad.flush()
        self._drain(now)

    def _remember(self, samples):
        """Write fed audio into the ring at absolute positions fed..fed+n."""
        size = len(self._ring)
        pos = self.fed                      # absolute position of samples[0]
        if len(samples) > size:             # one huge feed (a file at once): only its tail can matter
            pos += len(samples) - size
            samples = samples[-size:]
        n, i = len(samples), pos % size
        first = min(n, size - i)
        self._ring[i:i + first] = samples[:first]
        self._ring[:n - first] = samples[first:n]

    def _recall(self, a, b):
        """Audio at absolute sample positions [a, b) if still in the ring."""
        size = len(self._ring)
        a = max(a, self.fed - size)
        if b <= a:
            return np.zeros(0, dtype=np.float32)
        i, j = a % size, b % size
        return self._ring[i:i + (b - a)].copy() if i < j or b - a <= size - i else np.concatenate([self._ring[i:], self._ring[:j]])

    def _second_look(self, lo, start):
        """Speech regions [(a, b)] in absolute samples inside [lo, start) that a fresh VAD finds (union of 2 phases)."""
        win = self._recall(lo, start)
        lo = start - len(win)
        found = []
        for off in (0, 256):
            vad = make_vad()            # fresh each time: front.start counts from creation (reset() keeps counting)
            x = np.concatenate([win[off:], np.zeros(int(config.VAD_MIN_SILENCE * config.RATE) + 1600, np.float32)])
            for i in range(0, len(x), 1600):            # fed like the live stream (whole-array feeds differ)
                vad.accept_waveform(x[i:i + 1600])
            vad.flush()
            while not vad.empty():
                a = lo + off + vad.front.start
                if a < start:
                    found.append((a, min(a + len(vad.front.samples), start)))
                vad.pop()
        found.sort()
        merged = []
        for a, b in found:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return merged

    def _emit(self, start, samples, now):
        pre = self._recall(max(start - self._pre, self._last_end), start)
        self._last_end = start + len(samples)
        if len(pre):
            samples, start = np.concatenate([pre, samples]), start - len(pre)
        ts = now - (self.fed - start) / config.RATE
        self.seen += 1
        self.q.put((ts, samples, self.family))

    def _drain(self, now):
        R = config.RATE
        while not self.vad.empty():
            seg = self.vad.front
            samples = np.asarray(seg.samples, dtype=np.float32)
            start = seg.start
            self.vad.pop()
            lo = max(self._last_end, start - int(config.VAD_LOOKBACK * R))
            if self._look and start - lo >= R // 2:
                for a, b in self._second_look(lo, start):
                    if b >= start - int(0.3 * R):           # runs into this onset: the line really started at a
                        samples, start = np.concatenate([self._recall(a, start), samples]), a
                        self.rescued += 1
                        break
                    if b - a >= R // 2:                    # its own utterance the running VAD missed: its own line
                        self._emit(a, self._recall(a, b), now)
                        self.rescued += 1
            self._emit(start, samples, now)

    def wait_idle(self):
        self.q.join()

    def _spool_ok(self):
        if not self.spool:
            return False
        t = time.monotonic()
        if t - self._last_spool_check > 5:
            self._last_spool_check = t
            self._spool_full = spool_bytes() > config.SPOOL_MAX_BYTES
            if self._spool_full:
                log.error("audio spool over %d bytes: storing text without a voice embedding", config.SPOOL_MAX_BYTES)
        return not self._spool_full

    def _work(self):
        wdb = dbm.connect(self.db_path)
        fdb = None
        while True:
            ts, samples, family = self.q.get()
            try:
                text, ms = self._retry(lambda: self.asr.transcribe(samples), "transcribe")
                self.busy_ms += ms
                if not text:
                    continue
                raw = text
                try:
                    from . import vocab
                    text, fixed = vocab.apply(wdb, text)      # B3-1: Fred's vocabulary, learned from his fixes
                except Exception:
                    fixed = False
                dur = len(samples) / config.RATE
                from . import textguard
                gtext, gnoise, why = textguard.check(text, dur)     # E3: Whisper loops / impossible rates / lone fillers
                if gtext != text:
                    text, fixed = gtext, True                       # the original stays in text_orig
                if why:
                    log.info("text guard: %s", why)
                if family:
                    # B4-14 family archive: its own DB file, no voice clip, no live push, nothing of it in the logs
                    if fdb is None:
                        fdb = dbm.connect(config.FAMILY_DB)
                        os.chmod(config.FAMILY_DB, 0o600)
                    self._retry(lambda: dbm.insert_segment(fdb, ts, dur, text, None, self.asr.name, ms), "store")
                    self.stored += 1
                    continue
                name = None
                if self._spool_ok() and not gnoise:                 # a hallucination is not a voice sample
                    try:            # the voice clip is optional: a spool failure must never cost the text
                        name = f"{int(ts * 1000)}-{os.getpid()}-{next(self._n)}-{uuid.uuid4().hex[:8]}.wav"
                        path = config.SPOOL / (name + ".part")
                        sf.write(path, samples, config.RATE, subtype="PCM_16", format="WAV")
                        os.rename(path, config.SPOOL / name)
                    except Exception:
                        log.exception("spool write failed: storing the text without a voice clip")
                        name = None
                sid, diar = self._retry(lambda: dbm.insert_segment(wdb, ts, dur, text, name, self.asr.name, ms, source=self.source), "store")
                if fixed:
                    wdb.execute("UPDATE segments SET text_orig=? WHERE id=?", (raw, sid))
                if gnoise:
                    wdb.execute("UPDATE segments SET noise=1, diar=2, audio=NULL WHERE id=?", (sid,))   # hidden, never deleted
                self.ids.append(sid)
                if name and diar != 0:
                    (config.SPOOL / name).unlink(missing_ok=True)   # noise lines are not diarized
                self.stored += 1
                notify.send({"t": "seg", "id": sid})
                log.info("#%d %.1fs asr=%dms %s", sid, dur, ms, text[:80])
            except Exception:
                self.failed += 1
                log.exception("segment failed after retries (audio dropped, service continues)")
                if time.monotonic() - self._last_alert > 1800:      # loud, but at most one alert per 30 min
                    self._last_alert = time.monotonic()
                    from . import watchdog
                    watchdog.alert("Ramblr dropped a line", f"{self.failed} segment(s) failed after retries on "
                                   f"{getattr(self.asr, 'name', '?')}; see journalctl --user -u ramblr-capture")
            finally:
                self.q.task_done()

    @staticmethod
    def _retry(fn, what, tries=3):
        """NPU hiccups and SQLite busy timeouts are usually transient: try again before dropping a line."""
        for i in range(tries):
            try:
                return fn()
            except Exception:
                if i == tries - 1:
                    raise
                log.warning("%s failed (try %d of %d), retrying", what, i + 1, tries)
                time.sleep(0.5 * (i + 1))
