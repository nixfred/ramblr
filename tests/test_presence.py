"""B2-4: who's in the room from voices (TV excluded, unknowns pooled), quiet hours as a flag."""
from datetime import datetime

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAMBLR_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("RAMBLR_STATE", str(tmp_path / "state.json"))
    import importlib
    from ramblr import config
    importlib.reload(config)
    from ramblr import db, state, presence, speakers
    for m in (db, state, speakers, presence):
        importlib.reload(m)
    return {"db": db.connect(), "dbm": db, "p": presence}


T = datetime(2026, 10, 23, 9, 0).timestamp()


def say(env, t, spk, media=0):
    sid, _ = env["dbm"].insert_segment(env["db"], t, 3.0, "words", None, "t", 1)
    env["db"].execute("UPDATE segments SET speaker_id=?, diar=1, media=? WHERE id=?", (spk, media, sid))


def test_presence_spans_merge_and_exclude_tv(env):
    d, p = env["db"], env["p"]
    fred = env["dbm"].new_speaker(d, "Fred", 1)
    anon = env["dbm"].new_speaker(d)
    say(env, T, fred)
    say(env, T + 600, fred)          # 10 min later: still the same stay
    say(env, T + 4000, fred)         # over an hour later: a new stay
    say(env, T + 100, anon, media=1) # a TV voice: never present
    say(env, T + 200, anon)          # an unnamed room voice: pooled
    a = datetime(2026, 10, 23).timestamp()
    ppl = p.intervals(d, a, a + 86400)
    f = next(x for x in ppl if x["name"] == "Fred")
    assert len(f["spans"]) == 2 and f["spans"][0] == [T - 300, T + 603 + 300]
    u = next(x for x in ppl if x["key"] == p.UNKNOWN)
    assert u["lines"] == 1 and u["name"] == "Unknown voices"


def test_quiet_hours_flag_only(env):
    p = env["p"]
    assert p.is_quiet(datetime(2026, 10, 23, 23, 30).timestamp())
    assert p.is_quiet(datetime(2026, 10, 23, 3, 0).timestamp())
    assert not p.is_quiet(datetime(2026, 10, 23, 12, 0).timestamp())
    assert len(p.quiet_spans("2026-10-23")) == 2                     # wraps midnight
    p.set_quiet(True, "13:00", "14:00")
    assert p.is_quiet(datetime(2026, 10, 23, 13, 30).timestamp()) and not p.is_quiet(datetime(2026, 10, 23, 23, 30).timestamp())
    p.set_quiet(False, "13:00", "14:00")
    assert not p.is_quiet(datetime(2026, 10, 23, 13, 30).timestamp()) and p.quiet_spans("2026-10-23") == []
    with pytest.raises(ValueError):
        p.set_quiet(True, "25:00", "14:00")
