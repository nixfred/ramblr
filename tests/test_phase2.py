"""Phase 2: dates, action-item storage/dedupe, mute state, mic fallback, never-delete triggers."""
import json
import sqlite3
from datetime import datetime

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAMBLR_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("RAMBLR_STATE", str(tmp_path / "state.json"))
    import importlib
    from ramblr import config
    importlib.reload(config)
    from ramblr import db, state, nightly, dates, mics
    for m in (db, state, nightly, dates, mics):
        importlib.reload(m)
    return {"db": db.connect(), "dbm": db, "state": state, "nightly": nightly, "dates": dates, "mics": mics}


def add(env, ts, text, spk=None):
    sid, _ = env["dbm"].insert_segment(env["db"], ts, 3.0, text, None, "t", 1)
    if spk:
        env["db"].execute("UPDATE segments SET speaker_id=?, diar=1 WHERE id=?", (spk, sid))
    return sid


def test_dates(env):
    r = env["dates"].resolve
    sat = datetime(2026, 10, 24, 15).timestamp()
    assert r("tomorrow", sat) == "2026-10-25"
    assert r("by Friday", sat) == "2026-10-30"
    assert r("next week", sat) == "2026-10-26"
    assert r("tonight", sat) == "2026-10-24"
    assert r("in 3 days", sat) == "2026-10-27"
    assert r("sometime", sat) is None
    assert r("on Saturday", sat) == env["dates"].SAME_WEEKDAY
    assert env["dates"].valid_iso("2026-10-01", sat) is None          # before the day it was said
    assert env["dates"].valid_iso("2026-11-02", sat) == "2026-11-02"


def test_action_items_validate_and_dedupe(env):
    d = env["db"]
    t = datetime(2026, 10, 23, 9).timestamp()
    a, b = add(env, t, "I'll send Tom the photos tomorrow"), add(env, t + 10, "ok")
    ts_of = {a: t, b: t + 10}
    items = [{"owner": "Fred", "task": "Send Tom the photos", "due_phrase": "tomorrow", "line_ids": [a]},
             {"owner": "Fred", "task": "Invented", "line_ids": [9999]},          # untraceable: dropped
             {"owner": "", "task": "", "line_ids": [a]}]                        # empty: dropped
    new = env["nightly"].store_items(d, "2026-10-23", items, {a, b}, ts_of, "m")
    assert len(new) == 1
    row = d.execute("SELECT * FROM action_items").fetchone()
    assert row["due"] == "2026-10-24" and json.loads(row["line_ids"]) == [a]
    again = [{"owner": "Fred", "task": "Send the lake photos to Tom", "line_ids": [a, b]}]   # reworded re-run
    assert env["nightly"].store_items(d, "2026-10-23", again, {a, b}, ts_of, "m") == []


def test_never_delete(env):
    d = env["db"]
    t = datetime(2026, 10, 23, 9).timestamp()
    a = add(env, t, "x")
    env["nightly"].store_items(d, "2026-10-23", [{"owner": "F", "task": "do it", "line_ids": [a]}], {a}, {a: t}, "m")
    d.execute("INSERT INTO summaries(day,version,created,body) VALUES('2026-10-23',1,0,'{}')")
    for table in ("segments", "action_items", "summaries"):
        with pytest.raises(sqlite3.IntegrityError):
            d.execute(f"DELETE FROM {table}")


def test_mute_state_roundtrip(env):
    st = env["state"]
    assert st.load()["muted"] is False
    st.update(by="test", muted=True)
    assert st.load()["muted"] is True and st.load()["by"] == "test"
    st.update(by="test", source="mic2")
    assert st.load()["muted"] is True and st.load()["source"] == "mic2"


def test_mic_fallback(env, monkeypatch):
    m = env["mics"]
    monkeypatch.setattr(m.config, "SOURCE", "brio")
    mics = [{"name": "brio", "description": "BRIO", "available": True, "default": True},
            {"name": "usb", "description": "USB mic", "available": True, "default": False}]
    assert m.pick("usb", mics) == ("usb", "")
    assert m.pick(None, mics) == ("brio", "")
    gone = [mics[0]]
    src, warn = m.pick("usb", gone)
    assert src == "brio" and "not connected" in warn
    assert m.pick("usb", mics)[0] == "usb"          # comes back by itself on the next pick


def test_orphan_sweep_leaves_uploads_and_dirs(env, tmp_path, monkeypatch):
    import importlib, os, time as _t
    from ramblr import config, diarize
    importlib.reload(diarize)
    spool = config.SPOOL; spool.mkdir(parents=True, exist_ok=True)
    (spool / "uploads").mkdir(exist_ok=True)
    up = spool / "uploads" / "up-1.m4a"; up.write_bytes(b"x")
    orphan = spool / "123-1-1.wav"; orphan.write_bytes(b"x")
    old = _t.time() - 3600
    for p in (up, orphan, spool / "uploads"):
        os.utime(p, (old, old))
    diarize.sweep_orphans(env["db"])
    assert up.exists() and (spool / "uploads").is_dir() and not orphan.exists()
