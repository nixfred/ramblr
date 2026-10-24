"""B3-15: 24 h compressed audio store, play clips, and retention that dry-runs before it deletes, in scope only."""
import importlib
import json
import os
import time

import numpy as np
import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAMBLR_DATA", str(tmp_path))
    monkeypatch.setenv("RAMBLR_AUDIO24_DIR", str(tmp_path / "audio-24h"))
    from ramblr import config
    importlib.reload(config)
    from ramblr import db, audio24, retention
    for m in (db, audio24, retention):
        importlib.reload(m)
    return {"db": db.connect(), "a": audio24, "r": retention, "dir": tmp_path / "audio-24h", "tmp": tmp_path}


def tone(sec):
    t = np.arange(int(16000 * sec)) / 16000
    return (0.3 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16).tobytes()


def test_writer_chunks_split_on_gap_and_clip_plays(env):
    w = env["a"].Writer()
    now = time.time() - 60
    for i in range(20):                                   # 2 s of audio in 100 ms pieces
        w.write(tone(0.1), now + (i + 1) * 0.1)
    for i in range(10):                                   # a 10 s gap (mute), then 1 s more
        w.write(tone(0.1), now + 12 + (i + 1) * 0.1)
    w.close()
    ch = env["a"].chunks()
    assert len(ch) == 2 and all(p.suffix == ".opus" for _, p in ch)
    assert (os.stat(env["dir"]).st_mode & 0o777) == 0o700
    clip = env["a"].clip(now + 0.5, 1.0)
    assert clip and clip[:4] == b"OggS"
    assert env["a"].clip(now - 90000, 1.0) is None        # older than 24 h: no audio, ever


def _old(env, name, hours=30, content=b"x"):
    d = env["dir"] / "2026-10-20"
    d.mkdir(parents=True, exist_ok=True)
    start = time.time() - hours * 3600
    p = d / name.format(ms=int(start * 1000))
    p.write_bytes(content)
    os.utime(p, (start + 700, start + 700))
    return p


def test_retention_dry_run_then_verify_then_delete_only_scope(env):
    db, r = env["db"], env["r"]
    old = _old(env, "{ms}.opus")
    young = _old(env, "{ms}.opus", hours=2)
    other = _old(env, "{ms}.txt")                          # not audio: never touched
    outside = env["tmp"] / "ears.db"; outside.write_bytes(b"db")
    first = r.run(db)
    assert first["mode"] == "dry-run" and first["deleted"] == 0 and old.exists()
    log = [json.loads(l) for l in open(r.LOG)]
    assert [x["action"] for x in log if x["path"] == str(old)] == ["would delete"]
    assert all(x["path"] != str(young) for x in log)
    v = r.verify(db)
    assert v["enabled"] is True
    second = r.run(db)
    assert second["mode"] == "delete" and not old.exists()
    assert young.exists() and other.exists() and outside.exists()


def test_verify_refuses_empty_or_bad_log(env):
    db, r = env["db"], env["r"]
    assert r.verify(db)["enabled"] is False                # no log yet
    r.run(db)                                              # nothing old: empty dry run
    assert r.verify(db)["enabled"] is False
    link = env["dir"] / "2026-10-20"; link.mkdir(parents=True, exist_ok=True)
    target = env["tmp"] / "precious.opus"; target.write_bytes(b"keep")
    lk = link / f"{int((time.time() - 40 * 3600) * 1000)}.opus"; os.symlink(target, lk)
    out = r.run(db)
    assert out["refused"] == 1 and r.verify(db)["enabled"] is False and target.exists()


def test_auto_verify_only_when_armed_then_flips_on_first_clean_candidates(env):
    db, r = env["db"], env["r"]
    young = _old(env, "{ms}.opus", hours=2)
    t = r.tick(db)                                         # not armed: hourly tick never verifies
    assert t["verify"] is None and t["pass"]["mode"] == "dry-run"
    v = r.verify(db, arm_auto=True)                        # Fred's go via the CLI: arms, 0 candidates so far
    assert v == {"enabled": False, "armed": True, "why": v["why"], "files": []} and "nothing older" in v["why"]
    assert r.tick(db)["pass"]["candidates"] == 0
    old = _old(env, "{ms}.opus", hours=30)
    other = _old(env, "{ms}.txt", hours=30)
    first = r.tick(db)                                     # first dry run with real candidates: listed, nothing deleted
    assert first["verify"]["enabled"] is False and first["pass"]["mode"] == "dry-run" and old.exists()
    second = r.tick(db)                                    # next tick verifies that list, then deletes in scope only
    assert second["verify"]["enabled"] is True and [f["path"] for f in second["verify"]["files"]] == [str(old)]
    assert second["pass"]["mode"] == "delete" and second["pass"]["deleted"] == 1
    assert not old.exists() and young.exists() and other.exists()
    st = r.status(db)
    assert st["delete_enabled"] and st["auto_verify_armed"] and st["verified"]["list"][0]["path"] == str(old)
