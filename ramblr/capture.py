"""Always-on service: mic (PipeWire/Pulse client) -> Silero VAD -> Whisper on the NPU -> SQLite.
Mute and the chosen mic come from ~/.config/ramblr/state.json (see state.py). Muted means parec is stopped:
nothing is recorded at all. Switching mics restarts only the parec child, never the service or Whisper."""
import logging
import select
import signal
import subprocess
import sys
import time

import numpy as np

from . import audio24, config, db as dbm, mics, notify, state
from .asr import NpuWhisper
from .pipeline import Ingest

log = logging.getLogger("ramblr.capture")
CHUNK = 3200  # bytes = 100 ms of s16le mono 16 kHz


def start_parec(source):
    cmd = ["parec", "-d", source, f"--rate={config.RATE}", "--channels=1", "--format=s16le",
           "--latency-msec=100", "--client-name=ramblr", "--stream-name=ramblr room transcription"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    log.warning("capturing %s via parec pid %d", source, p.pid)
    return p


def stop_parec(p):
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


class Capture:
    def __init__(self, ing):
        self.stopping = False
        self.ing = ing
        self.proc = None
        self.source = None          # what parec is recording now (None = stopped)
        self.mode = "starting"      # listening | muted | error
        self.warning = ""
        self.st, self.st_mtime = state.load(), -1
        self.next_pick = 0.0
        self.failed = {}            # source -> monotonic time parec last died on it
        self.started = time.time()
        self.last_hb = 0.0
        self.a24 = audio24.Writer()

    def publish(self):
        d = self.ing.db
        try:
            from . import vocab
            self.ing.asr.prompt = ", ".join(vocab.prompt_words(d))     # used only if the device takes prompts
        except Exception:
            pass
        asr = self.ing.asr
        dbm.set_meta(d, "asr_model", getattr(asr, "name", ""))      # follows a fallback (asr.py)
        dbm.set_meta(d, "asr_errors", getattr(asr, "errors", 0))
        dbm.set_meta(d, "asr_fallback", getattr(asr, "fell_back", None) or "")
        dbm.set_meta(d, "capture_failed", self.ing.failed)
        dbm.set_meta(d, "capture_heartbeat", time.time())
        dbm.set_meta(d, "capture_started", self.started)
        dbm.set_meta(d, "capture_source", self.source or "")
        dbm.set_meta(d, "capture_state", self.mode)
        dbm.set_meta(d, "capture_warning", self.warning)
        dbm.set_meta(d, "capture_chosen", self.st.get("source") or "")
        dbm.set_meta(d, "capture_family", "1" if self.st.get("family") else "0")
        notify.send({"t": "cap"})

    def reconcile(self, force=False):
        """Bring parec in line with state.json and the mics that exist right now."""
        m = state.mtime()
        changed = m != self.st_mtime
        if changed:
            self.st, self.st_mtime = state.load(), m
        now = time.monotonic()
        if not (changed or force or now >= self.next_pick):
            return
        fam = bool(self.st.get("family"))                  # B4-14
        if fam != self.ing.family:
            self.ing.flush()       # routing boundary: speech so far keeps the mode it was spoken in
            self.ing.family = fam
        if self.st.get("muted"):
            want, warn, mode = None, "", "muted"
        else:
            listing = mics.list_sources()
            # a source parec just died on counts as missing for 10 s, so an unplug falls back at once
            gone = {s for s, t in self.failed.items() if now - t < 10}
            usable = [x if x["name"] not in gone else {**x, "available": False} for x in listing]
            want, warn = mics.pick(self.st.get("source"), usable)
            mode = "listening"
        self.next_pick = now + (3 if warn else 10)
        if want != self.source:
            self.a24.close()           # mute or mic switch: the 24 h store starts a new chunk (muted time is never written)
            if self.proc:
                stop_parec(self.proc)
                self.ing.flush()       # finish the segment that was being spoken when the switch happened
            self.proc = start_parec(want) if want else None
            log.warning("capture %s -> %s (%s)%s", self.source, want, mode, f" warning: {warn}" if warn else "")
            self.source = want
            self.next_pick = now + 1.5   # check soon that the new stream really runs
        if mode != self.mode or warn != self.warning or changed:
            self.mode, self.warning = mode, warn
            self.publish()

    def run(self):
        buf = b""
        last_data = time.monotonic()
        self.reconcile(force=True)
        while not self.stopping:
            self.reconcile()
            t = time.monotonic()
            if t - self.last_hb > 15:
                self.last_hb = t
                self.publish()
            if self.proc is None:
                time.sleep(0.5)
                last_data = time.monotonic()
                continue
            r, _, _ = select.select([self.proc.stdout], [], [], 1)
            if not r:
                if time.monotonic() - last_data > 30:
                    log.error("no audio from parec on %s for 30 s, restarting the stream", self.source)
                    self.failed[self.source] = time.monotonic()
                    stop_parec(self.proc)
                    self.proc, self.source = None, None
                    self.reconcile(force=True)
                    last_data = time.monotonic()
                continue
            data = self.proc.stdout.read(CHUNK)
            if not data:
                err = self.proc.stderr.read().decode(errors="replace").strip()
                log.error("parec on %s exited: %s", self.source, err[:200])
                self.failed[self.source] = time.monotonic()
                self.proc.wait()
                self.proc, self.source = None, None
                self.mode = "error"
                self.reconcile(force=True)
                if self.proc is None and not self.st.get("muted"):
                    time.sleep(1)
                continue
            last_data = time.monotonic()
            try:
                self.a24.write(data, time.time())
            except Exception:
                log.exception("audio-24h write failed (capture continues)")
            buf += data
            n = len(buf) - (len(buf) % 2)
            if n:
                samples = np.frombuffer(buf[:n], dtype=np.int16).astype(np.float32) / 32768.0
                buf = buf[n:]
                self.ing.feed(samples)
        self.shutdown()

    def shutdown(self, timeout=20):
        """SIGTERM: stop listening, finish the utterance in flight, let Whisper store what is queued, then exit."""
        stop_parec(self.proc)
        self.proc = None
        self.ing.flush()
        end = time.monotonic() + timeout
        while self.ing.q.unfinished_tasks and time.monotonic() < end:
            time.sleep(0.1)
        left = self.ing.q.unfinished_tasks
        (log.error if left else log.warning)("capture stopping: %d queued segment(s) %s", left, "abandoned after 20 s" if left else "all stored")
        self.a24.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stdout)
    asr = NpuWhisper()
    ing = Ingest(config.DB_PATH, asr)
    dbm.set_meta(ing.db, "asr_model", asr.name)
    dbm.set_meta(ing.db, "asr_device", asr.device)
    cap = Capture(ing)

    def bye(*_):
        cap.stopping = True        # the run loop notices within 1 s and shuts down in order (never re-enter the VAD here)
    signal.signal(signal.SIGTERM, bye)
    cap.run()


if __name__ == "__main__":
    main()
