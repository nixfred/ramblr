"""Extra mics over the tailnet (B4-19): another machine (vic, a Pi) streams its mic to Ramblr on gus.

  client (clients/ramblr-mic): parec or arecord -> raw s16le 16 kHz mono -> curl -T - over HTTPS to
      https://gus.<tailnet>.ts.net:8798/api/mic/stream?device=<name>, 'Authorization: Bearer <RAMBLR_MIC_KEY>'.
      Encrypted twice: TLS from tailscale serve, inside WireGuard. The web binds 127.0.0.1 only, so the only way
      in from another machine is the tailnet.
  web (/api/mic/stream): checks the key and the device name, then relays the body to this service over a 0600
      unix socket. Whisper never loads in the web process.
  this service (ramblr-mics.service): one Ingest per device (Silero VAD -> Whisper on the NPU -> SQLite, lines
      tagged source "mic:<device>"), sharing one Whisper behind a lock. Loaded on the first stream, not at boot.

Same rules as the room mic: mute drops remote audio too (nothing is transcribed or kept while muted), family mode
sends lines to family.db, audio is never kept (tmpfs spool for the voice embedding only). Each device runs its own
media classifier (YAMNet on the NPU) and its lines are judged only by its own windows (Fred 8:03 AM).
Status is in memory only (`{"op": "status"}` on the socket); nothing about connections is written to disk.

Socket protocol: one JSON header line, the server answers "ok\\n" or "err <reason>\\n", then raw PCM until the
client half-closes; once every line is stored the server sends {"seconds": .., "lines": ..} and closes."""
import json
import logging
import os
import re
import socket
import socketserver
import sys
import threading
import time

import numpy as np

from . import config, state

log = logging.getLogger("ramblr.mics")
DEVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
RATE = 16000
CHUNK = 3200 * 2          # 0.2 s of s16le


class LockedASR:
    """One Whisper for every remote device: transcribe() calls are serialized."""

    def __init__(self, asr):
        self._asr, self._lock = asr, threading.Lock()
        self.name, self.device = asr.name, getattr(asr, "device", "")

    @property
    def prompt(self):
        return getattr(self._asr, "prompt", "")

    @prompt.setter
    def prompt(self, v):
        self._asr.prompt = v

    def transcribe(self, samples):
        with self._lock:
            return self._asr.transcribe(samples)


class Hub:
    def __init__(self, asr_factory=None, ingest_factory=None):
        self._asr_factory = asr_factory or self._npu
        self._ingest_factory = ingest_factory or self._ingest
        self._asr = None
        self._lock = threading.Lock()
        self.ingests = {}
        self.status = {}
        self._st, self._st_mtime = state.load(), state.mtime()

    @staticmethod
    def _npu():
        from .asr import NpuWhisper
        return NpuWhisper()

    @staticmethod
    def _ingest(asr, device):
        from .pipeline import Ingest
        return Ingest(config.DB_PATH, asr, spool=True, media=True, source=f"mic:{device}")   # its own TV detector

    def claim(self, device):
        """Ingest for a device that is not already streaming, or a reason string."""
        if not DEVICE_RE.match(device or ""):
            return "bad device name (a-z, 0-9, dash; up to 32)"
        with self._lock:
            if self.status.get(device, {}).get("connected"):
                return "device already streaming"
            if self._asr is None:
                self._asr = LockedASR(self._asr_factory())
            if device not in self.ingests:
                self.ingests[device] = self._ingest_factory(self._asr, device)
            st = self.status.setdefault(device, {"bytes": 0, "lines": 0})
            st.update(connected=True, since=time.time(), last=time.time())
            return self.ingests[device]

    def release(self, device):
        with self._lock:
            if device in self.status:
                self.status[device]["connected"] = False

    def switches(self):
        """(muted, family) from state.json, re-read when the file changes."""
        m = state.mtime()
        if m != self._st_mtime:
            self._st, self._st_mtime = state.load(), m
        return bool(self._st.get("muted")), bool(self._st.get("family"))

    def stream(self, device, ing, read):
        """Feed PCM from read(n) -> bytes until it returns b''. Returns (seconds of audio received, lines stored)."""
        st = self.status[device]
        rest = b""
        got = 0
        before = ing.stored
        while True:
            data = read(CHUNK)
            if not data:
                break
            data = rest + data
            cut = len(data) - len(data) % 2           # a chunk can split a sample
            data, rest = data[:cut], data[cut:]
            got += len(data)
            st["bytes"] += len(data)
            st["last"] = time.time()
            muted, family = self.switches()
            if muted:
                continue                                # muted: dropped, never transcribed or kept
            if family != ing.family:
                ing.flush()                             # routing boundary, same as the room mic
                ing.family = family
            ing.feed(np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0)
        ing.flush()
        ing.wait_idle()
        st["lines"] += ing.stored - before
        return got / 2 / RATE, ing.stored - before

    def snapshot(self):
        with self._lock:
            return {d: {k: (round(v, 1) if isinstance(v, float) else v) for k, v in s.items()} for d, s in self.status.items()}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        hub = self.server.hub
        try:
            head = json.loads(self.rfile.readline(512) or b"{}")
        except ValueError:
            self.wfile.write(b"err bad header\n")
            return
        if head.get("op") == "status":
            self.wfile.write(json.dumps(hub.snapshot()).encode() + b"\n")
            return
        if int(head.get("rate", RATE)) != RATE:
            self.wfile.write(b"err only 16000 Hz s16le mono\n")
            return
        device = str(head.get("device", ""))
        ing = hub.claim(device)
        if isinstance(ing, str):
            self.wfile.write(f"err {ing}\n".encode())
            return
        try:
            self.wfile.write(b"ok\n")                  # inside the try: a dead relay must still release the device
            self.wfile.flush()
            log.info("mic:%s connected", device)
            secs, lines = hub.stream(device, ing, self.rfile.read1)
            log.info("mic:%s disconnected after %.0f s of audio, %d lines", device, secs, lines)
        except Exception:
            log.exception("mic:%s stream failed", device)
            secs, lines = None, None
        finally:
            hub.release(device)
        try:
            self.wfile.write(json.dumps({"seconds": secs if secs is None else round(secs, 1), "lines": lines}).encode() + b"\n")
        except OSError:
            pass


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path, hub):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        old = os.umask(0o177)
        try:
            super().__init__(str(path), Handler)
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        self.hub = hub


def status(timeout=2):
    """Snapshot from the running service, or None if it is not running."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(config.MICS_SOCK))
            s.sendall(b'{"op": "status"}\n')
            return json.loads(s.makefile("rb").readline() or b"{}")
    except (OSError, ValueError):
        return None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stdout)
    srv = Server(config.MICS_SOCK, Hub())
    log.warning("ramblr-mics ready on %s (Whisper loads on the first stream)", config.MICS_SOCK)
    srv.serve_forever()


if __name__ == "__main__":
    main()
