"""First-word clipping fix (Fred, 7:10 AM: mic:vic stored "Good morning" as "morning"). Ingest keeps a ring of
recent audio and prepends up to VAD_PREROLL s before every VAD onset, for every source (room mic and mic:*).
Temp DB only (conftest). The real-Whisper check runs when the NPU model is on this box."""
import importlib
from pathlib import Path

import numpy as np
import pytest

JFK = Path.home() / ".local/share/ramblr/test/jfk16.wav"


class StubASR:
    name, device = "stub", "CPU"

    def transcribe(self, samples):
        return "x", 1


def jfk_from_onset():
    import soundfile as sf
    x, _ = sf.read(str(JFK), dtype="float32")
    e = np.sqrt(np.mean(x[: len(x) // 160 * 160].reshape(-1, 160) ** 2, axis=1))
    return x[int(np.argmax(e > 0.02)) * 160:]


def make(asr, source):
    from ramblr import config, pipeline
    importlib.reload(pipeline)
    if not config.VAD_MODEL.exists() or not JFK.exists():
        pytest.skip("needs the Silero model and the JFK clip on this box")
    return pipeline.Ingest(config.DB_PATH, asr, spool=False, media=False, source=source)


def run(ing, clip, chunk=3200, t0=1792760000.0):
    for i in range(0, len(clip), chunk):                 # 0.2 s chunks, like the remote mic stream
        ing.feed(clip[i:i + chunk], now=t0 + (i + chunk) / 16000)
    ing.flush(now=t0 + len(clip) / 16000)
    ing.wait_idle()
    return [tuple(r) for r in ing.db.execute("SELECT ts, dur, text FROM segments WHERE source=? ORDER BY id", (ing.source,))]


def test_ring_recall_wraps_and_survives_one_huge_feed():
    ing = make(StubASR(), "t")
    size = len(ing._ring)
    sig = np.arange(size * 2 + 777, dtype=np.float32)
    ing._remember(sig[:size + 5]); ing.fed += size + 5           # one feed bigger than the ring
    assert np.array_equal(ing._recall(ing.fed - 100, ing.fed), sig[size + 5 - 100:size + 5])
    for k in range(size + 5, len(sig), 3333):                    # then ordinary chunks across the wrap point
        part = sig[k:k + 3333]; ing._remember(part); ing.fed += len(part)
    assert np.array_equal(ing._recall(ing.fed - 6400, ing.fed - 100), sig[len(sig) - 6400:len(sig) - 100])
    assert len(ing._recall(0, 10)) == 0                          # long gone: nothing, never wrong audio


def test_speech_at_sample_0_and_mid_stream_segments_start_at_or_before_onset():
    from ramblr import config
    speech = jfk_from_onset()
    at0 = run(make(StubASR(), "t:at0"), speech)
    assert at0 and at0[0][0] <= 1792760000.0 + 0.001               # starts at sample 0, not after it
    noise = np.random.default_rng(1).normal(0, 0.003, 24000).astype(np.float32)
    mid = run(make(StubASR(), "t:mid"), np.concatenate([noise, speech]))
    onset = 1792760000.0 + 1.5
    assert mid[0][0] <= onset - 0.2                               # pre-roll reaches back before the onset
    assert mid[0][0] >= onset - config.VAD_PREROLL - 0.001
    ends = [ts + dur for ts, dur, _ in mid]
    assert all(mid[i + 1][0] >= ends[i] - 1e-6 for i in range(len(mid) - 1))   # never overlaps the previous line


def test_first_word_survives_real_whisper_mic_source():
    from ramblr import config
    if not Path(config.WHISPER_DIR).exists():
        pytest.skip("needs the NPU Whisper model")
    from ramblr.asr import NpuWhisper
    w = NpuWhisper()
    speech = jfk_from_onset()
    noise = np.random.default_rng(1).normal(0, 0.003, 24000).astype(np.float32)
    at0 = " ".join(r[2] for r in run(make(w, "mic:test0"), speech)).lower()
    mid = " ".join(r[2] for r in run(make(w, "mic:test"), np.concatenate([noise, speech]))).lower()
    assert at0.startswith("and so")                               # speech from sample 0: "And" kept
    assert "ask not" in mid                                       # was "not" before the pre-roll (measured)


def test_vocab_applies_to_remote_mic_lines():
    from ramblr import config, db as dbm, vocab
    importlib.reload(vocab)

    class Says(StubASR):
        def transcribe(self, samples):
            return "continue working on Ramblor", 1
    ing = make(Says(), "mic:vic")
    vocab.add_word(ing.db, "Ramblr", wrong="Ramblor")
    rows = run(ing, jfk_from_onset()[:16000 * 3])
    assert rows and all(r[2] == "continue working on Ramblr" for r in rows)
    assert ing.db.execute("SELECT text_orig FROM segments WHERE source='mic:vic' LIMIT 1").fetchone()[0] == "continue working on Ramblor"
