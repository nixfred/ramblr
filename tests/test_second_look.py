"""Second look (2026-10-24 11:50): over music the running VAD missed "Hey Larry, have Grockbot" and started the next
line late. A stub main VAD emits one scripted late onset over the real JFK clip; the fresh look-VADs must find the
speech before it. Temp DB only (conftest)."""
import importlib
from pathlib import Path

import numpy as np
import pytest

JFK = Path.home() / ".local/share/ramblr/test/jfk16.wav"
pytestmark = pytest.mark.skipif(not JFK.exists(), reason="JFK clip not present")
R = 16000


class FakeASR:
    name, device, prompt = "fake", "CPU", ""

    def transcribe(self, a):
        return "", 0


class Seg:
    def __init__(self, start, samples):
        self.start, self.samples = start, samples


class StubVAD:
    """Main VAD stand-in: swallows audio, emits exactly the segments we script."""
    def __init__(self):
        self.q = []

    def accept_waveform(self, x):
        pass

    def empty(self):
        return not self.q

    @property
    def front(self):
        return self.q[0]

    def pop(self):
        self.q.pop(0)

    def flush(self):
        pass


def ingest(monkeypatch, lookback):
    monkeypatch.setenv("RAMBLR_VAD_LOOKBACK", str(lookback))
    from ramblr import config, pipeline
    importlib.reload(config)
    importlib.reload(pipeline)
    ing = pipeline.Ingest(config.DB_PATH, FakeASR(), spool=False, media=False)
    ing.vad = StubVAD()
    emitted = []
    ing.q.put = lambda item: emitted.append(item)            # (ts, samples, family): look at what would be transcribed
    import soundfile as sf
    audio = np.concatenate([np.zeros(R, np.float32), sf.read(str(JFK), dtype="float32")[0], np.zeros(R, np.float32)])
    return ing, audio, emitted


def run(ing, audio, onset_s, end_s=12.0):
    """Feed everything, then let the main VAD report one segment that starts late at onset_s (absolute seconds)."""
    for i in range(0, len(audio), 1600):
        ing.feed(audio[i:i + 1600].copy(), now=1000.0 + (i + 1600) / R)
    a, b = int(onset_s * R), int(end_s * R)
    ing.vad.q.append(Seg(a, audio[a:b]))
    ing._drain(1000.0 + len(audio) / R)


def test_late_onset_is_extended_and_missed_lines_come_back(monkeypatch):
    ing, audio, out = ingest(monkeypatch, 8.0)
    run(ing, audio, onset_s=7.4)            # JFK's third phrase really starts at 6.41 s here (1 s lead-in)
    starts = [round(ts - 1000.0, 2) for ts, _, _ in out]
    assert len(out) == 3 and ing.rescued == 3, starts        # two missed phrases + the extended one
    assert abs(starts[0] - (1.32 - 0.4)) < 0.4 and abs(starts[1] - (4.3 - 0.4)) < 0.4   # own lines (with pre-roll)
    assert starts[2] < 7.4 - 0.5                              # the late line now starts before its old onset
    assert starts == sorted(starts)


def test_lookback_off(monkeypatch):
    ing, audio, out = ingest(monkeypatch, 0)
    run(ing, audio, onset_s=7.4)
    assert len(out) == 1 and ing.rescued == 0


def test_never_reaches_back_into_the_previous_line(monkeypatch):
    ing, audio, out = ingest(monkeypatch, 8.0)
    ing._last_end = int(5.8 * R)            # the first two phrases were already transcribed
    run(ing, audio, onset_s=7.4)
    assert len(out) == 1 and ing.rescued == 1                # only the extension, nothing re-emitted
    assert out[0][0] - 1000.0 >= 5.8 - 1e-6
