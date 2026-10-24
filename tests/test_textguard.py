"""E3 (Fred 7:57 AM): Whisper repetition loops collapse, impossible speech rates and lone fillers on long segments
become noise (hidden, never deleted), normal speech is untouched. Temp DB only."""
import importlib
from pathlib import Path

import numpy as np
import pytest

from ramblr import textguard as g


def test_loops_collapse_and_real_speech_is_untouched():
    t, nz, why = g.check(", ".join(["the big things"] * 120), 7.7)
    assert t.startswith("the big things …") and len(t) < 40 and not nz and "loop" in why
    assert g.check("Shh. " * 140, 2.8)[0] == "Shh. …"
    t, nz, _ = g.check("And I sure can get right here and I, " + "I, " * 200, 10.1)
    assert t.endswith("I …") and len(t) < 60
    for ok in ("Come on, folks. Come on, folks.", "Have the finance bot look up all my subscriptions", "No no no."):
        assert g.check(ok, 4.0) == (ok, False, "")


def test_impossible_rate_and_lone_filler_on_long_segment_are_noise():
    assert g.check("x" * 400, 4.0)[1] is True                         # 100 chars/s
    assert g.check("the", 17.8)[1] is True and g.check("The", 15.3)[1] is True
    assert g.check("the", 5.8)[1] is False                            # short: could be real
    assert g.check("I'm going to go with this side of the museum as well. Hopefully we'll have a messy budget.", 3.1)[1] is False  # 29 chars/s, real
    assert g.check("Hello", 12.0)[1] is False                         # a real word, not a filler


def test_ingest_applies_the_guard_and_keeps_the_original():
    from ramblr import config, pipeline
    importlib.reload(pipeline)
    jfk = Path.home() / ".local/share/ramblr/test/jfk16.wav"
    if not config.VAD_MODEL.exists() or not jfk.exists():
        pytest.skip("needs the Silero model and the JFK clip")
    import soundfile as sf

    class Loop:
        name, device = "loop", "CPU"
        def transcribe(self, s):
            return ", ".join(["the big things"] * 100), 1
    ing = pipeline.Ingest(config.DB_PATH, Loop(), spool=False, media=False)
    x = sf.read(str(jfk), dtype="float32")[0][:16000 * 4]
    for i in range(0, len(x), 3200):
        ing.feed(x[i:i + 3200], now=1792760000 + (i + 3200) / 16000)
    ing.flush(now=1792760000 + len(x) / 16000)
    ing.wait_idle()
    r = ing.db.execute("SELECT text, text_orig, noise FROM segments").fetchone()
    assert r["text"].startswith("the big things …") and len(r["text_orig"]) > 1000 and r["noise"] == 0
