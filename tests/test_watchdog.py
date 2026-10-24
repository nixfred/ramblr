"""Outage 2026-10-24: distil-large-v3 rejected the vocab prompt on the NPU and every later call raised roi_end.
Fake pipelines only (no NPU), temp DB and temp marker/state files only (conftest)."""
import importlib
import time
from types import SimpleNamespace

import numpy as np
import pytest

ROI = RuntimeError("Exception from src/inference/src/cpp/infer_request.cpp:246:\nCheck '*roi_end <= *max_dim' failed")


class FakePipe:
    """Like distil on the NPU: a prompted call raises and poisons this pipeline instance for good."""
    def __init__(self, model_dir, broken=False):
        self.model_dir, self.broken, self.poisoned, self.calls = model_dir, broken, False, 0

    def get_generation_config(self):
        return SimpleNamespace(max_new_tokens=0, initial_prompt=None)

    def generate(self, a, cfg=None, max_new_tokens=None):
        self.calls += 1
        if self.broken or self.poisoned:
            raise ROI
        if cfg is not None and getattr(cfg, "initial_prompt", None):
            self.poisoned = True
            raise ROI
        return SimpleNamespace(texts=[f"hello from {self.model_dir.name}"])


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "distil").mkdir()
    (tmp_path / "base.en").mkdir()
    monkeypatch.setenv("RAMBLR_WHISPER", str(tmp_path / "distil"))
    monkeypatch.setenv("RAMBLR_WHISPER_FALLBACK", str(tmp_path / "base.en"))
    monkeypatch.setenv("RAMBLR_WHISPER_CACHE", str(tmp_path / "cache"))
    from ramblr import config, asr, watchdog
    importlib.reload(config)
    importlib.reload(asr)
    importlib.reload(watchdog)
    return SimpleNamespace(config=config, asr=asr, watchdog=watchdog, tmp=tmp_path)


def whisper(env, broken=(), device="NPU", made=None):
    made = made if made is not None else []

    class W(env.asr.NpuWhisper):
        def _make_pipe(self, model_dir):
            p = FakePipe(model_dir, broken=model_dir.name in broken)
            made.append(p)
            return p
    return W(device=device), made


SPEECH = np.full(16000, 0.1, dtype=np.float32)


def test_npu_never_sends_the_prompt(env):
    w, made = whisper(env)
    w.prompt = "Ramblr, Biscuit, Jordan"
    for _ in range(3):
        assert w.transcribe(SPEECH)[0] == "hello from distil"
    assert w.name == "distil" and w.errors == 0 and len(made) == 1


def test_rejected_prompt_rebuilds_the_pipeline(env):
    """On a device where prompts are tried, a rejected prompt must not poison every later call (the 11:35 bug)."""
    w, made = whisper(env, device="CPU")
    w.prompt = "Ramblr"
    assert w.transcribe(SPEECH)[0] == "hello from distil"
    assert w.transcribe(SPEECH)[0] == "hello from distil"
    assert w.prompt_ok is False and len(made) == 2 and w.name == "distil"


def test_old_code_path_would_poison(env):
    """Sanity: the fake reproduces the outage (prompted call, then plain calls on the same pipe fail)."""
    p = FakePipe(env.tmp / "distil")
    with pytest.raises(RuntimeError):
        p.generate(SPEECH, SimpleNamespace(initial_prompt="x"))
    with pytest.raises(RuntimeError):
        p.generate(SPEECH, max_new_tokens=440)


def test_broken_model_falls_back_once_and_marks(env):
    w, made = whisper(env, broken=("distil",))
    text, _ = w.transcribe(SPEECH)
    assert text == "hello from base.en" and w.name == "base.en" and w.fell_back
    m = env.asr.fallback_marker()
    assert m["model"] == "distil" and "roi_end" in m["reason"]
    assert w.transcribe(SPEECH)[0] == "hello from base.en"
    assert len(made) == 3            # first load, one rebuild, the fallback; nothing after


def test_marker_starts_on_fallback_and_clear_lets_it_back(env, monkeypatch):
    env.asr.mark_fallback("distil", "test")
    w, made = whisper(env)
    assert w.name == "base.en" and [p.model_dir.name for p in made] == ["base.en"]
    monkeypatch.setattr("sys.argv", ["watchdog", "clear"])
    env.watchdog.main()
    w2, _ = whisper(env)
    assert w2.name == "distil"


def test_fallback_also_broken_raises_no_loop(env):
    w, made = whisper(env, broken=("distil", "base.en"))
    with pytest.raises(RuntimeError):
        w.transcribe(SPEECH)
    with pytest.raises(RuntimeError):
        w.transcribe(SPEECH)
    names = [p.model_dir.name for p in made]
    assert names.count("base.en") <= 3 and w.fallback_dir is None


def test_pipeline_stores_lines_through_a_fallback(env):
    from ramblr import pipeline, db as dbm
    importlib.reload(pipeline)
    w, _ = whisper(env, broken=("distil",))
    ing = pipeline.Ingest(env.config.DB_PATH, w, spool=False, media=False)
    ing.q.put((time.time(), SPEECH, False))
    ing.wait_idle()
    db = dbm.connect(env.config.DB_PATH)
    rows = db.execute("SELECT model, text FROM segments").fetchall()
    assert [(r["model"], r["text"]) for r in rows] == [("base.en", "hello from base.en")] and ing.failed == 0


# --- external watchdog -------------------------------------------------------------------------------------------

def setup_db(env, now, started_ago=3600, state="listening", speech_windows=20, hb_ago=10):
    from ramblr import db as dbm
    db = dbm.connect(env.config.DB_PATH)
    dbm.set_meta(db, "capture_state", state)
    dbm.set_meta(db, "capture_heartbeat", now - hb_ago)
    dbm.set_meta(db, "capture_started", now - started_ago)
    dbm.set_meta(db, "asr_model", "distil")
    for i in range(speech_windows):
        t1 = now - 30 - i * 3
        db.execute("INSERT INTO media_windows(ts0,ts1,music,tv,crowd,narration,speech,top,rms,source) VALUES(?,?,0,0,0,0,0.9,0,0.05,NULL)",
                   (t1 - 2.88, t1))
    return db


def test_check_speech_without_lines(env):
    now = time.time()
    db = setup_db(env, now)
    assert "no room line" in env.watchdog.check(db, now)
    from ramblr import db as dbm
    dbm.insert_segment(db, now - 120, 2.0, "we are talking", None, "distil", 100)
    assert env.watchdog.check(db, now) is None


def test_check_quiet_room_music_muted_and_fresh_start_are_fine(env):
    now = time.time()
    assert env.watchdog.check(setup_db(env, now, speech_windows=3), now) is None


def test_check_muted(env):
    now = time.time()
    assert env.watchdog.check(setup_db(env, now, state="muted", hb_ago=9999), now) is None


def test_check_fresh_start(env):
    now = time.time()
    assert env.watchdog.check(setup_db(env, now, started_ago=60), now) is None


def test_check_stale_heartbeat(env):
    now = time.time()
    assert "heartbeat" in env.watchdog.check(setup_db(env, now, speech_windows=0, hb_ago=600), now)


def test_tick_restarts_once_then_only_alerts_then_recovers(env):
    now = time.time()
    db = setup_db(env, now)
    restarts = []
    assert env.watchdog.tick(db, now, restart=lambda: restarts.append(1)) == "restarted"
    assert env.asr.fallback_marker()["model"] == "distil"
    assert env.watchdog.tick(db, now + 120, restart=lambda: restarts.append(1)) == "alerted"
    assert env.watchdog.tick(db, now + 240, restart=lambda: restarts.append(1)) == "waiting"
    assert restarts == [1]
    from ramblr import db as dbm
    dbm.insert_segment(db, now + 300, 2.0, "back again", None, "base.en", 100)
    assert env.watchdog.tick(db, now + 360, restart=lambda: restarts.append(1)) == "recovered"
    dbm.set_meta(db, "capture_heartbeat", now + 470)
    assert env.watchdog.tick(db, now + 480, restart=lambda: restarts.append(1)) == "ok"
    dbm.set_meta(db, "capture_heartbeat", now + 100)          # a new incident inside the hour: alert, no restart
    assert env.watchdog.tick(db, now + 600, restart=lambda: restarts.append(1)) == "alerted"
    assert env.watchdog.tick(db, now + 720, restart=lambda: restarts.append(1)) == "waiting"
    assert restarts == [1]
