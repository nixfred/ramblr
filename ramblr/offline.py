"""Offline test: push a WAV through the same VAD -> NPU Whisper -> SQLite path as the live service.
Usage: python -m ramblr.offline FILE [--db PATH] [--model DIR] [--at EPOCH]"""
import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from . import config

NPU_BUSY = Path("/sys/class/accel/accel0/device/npu_busy_time_us")


def npu_busy():
    try:
        return int(NPU_BUSY.read_text())
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--db", default=str(config.DATA / "test" / "offline.db"))
    ap.add_argument("--model", default=None)
    ap.add_argument("--at", type=float, default=None)
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    from .asr import NpuWhisper
    from .pipeline import Ingest
    asr = NpuWhisper(Path(a.model) if a.model else None)
    ing = Ingest(Path(a.db), asr)
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", a.file, "-ac", "1", "-ar", str(config.RATE),
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    base = a.at if a.at is not None else time.time()
    b0, w0, c0 = npu_busy(), time.monotonic(), time.process_time()
    step = config.RATE // 10
    for i in range(0, len(pcm), step):
        ing.feed(pcm[i:i + step], now=base + (i + step) / config.RATE)
    ing.flush(now=base + len(pcm) / config.RATE)
    ing.wait_idle()
    wall, cpu, b1 = time.monotonic() - w0, time.process_time() - c0, npu_busy()
    print(f"audio={len(pcm) / config.RATE:.1f}s wall={wall:.2f}s cpu={cpu:.2f}s model={asr.name} device={asr.device} "
          f"segments_seen={ing.seen} stored={ing.stored} asr_ms={ing.busy_ms} "
          f"npu_busy_delta_us={None if b0 is None or b1 is None else b1 - b0}")
    for r in ing.db.execute("SELECT id, ts, dur, text FROM segments ORDER BY id DESC LIMIT ?", (ing.stored,)):
        print(f"  #{r['id']} {r['dur']:.1f}s | {r['text']}")


if __name__ == "__main__":
    main()
