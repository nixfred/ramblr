"""B3-3: question hints (time words, people) and hybrid retrieval through the FTS5 word index (test DB only)."""
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def env():
    from ramblr import config, db, search
    import importlib
    importlib.reload(db); importlib.reload(search)
    return {"db": db.connect(config.DB_PATH), "dbm": db, "s": search}


NOW = datetime(2026, 10, 24, 15, 0).timestamp()    # a Saturday afternoon


def test_hints_time_words(env):
    h = env["s"].hints
    d0 = datetime(2026, 10, 24)
    assert h(env["db"], "what did we decide yesterday", NOW)["start"] == (d0 - timedelta(days=1)).timestamp()
    lw = h(env["db"], "anything about money last week", NOW)
    assert lw["start"] == datetime(2026, 10, 12).timestamp() and lw["end"] == datetime(2026, 10, 19).timestamp()
    tue = h(env["db"], "what did I say on Tuesday", NOW)
    assert tue["start"] == datetime(2026, 10, 20).timestamp()
    mo = h(env["db"], "what came up this morning", NOW)
    assert mo["start"] == d0.replace(hour=5).timestamp() and mo["end"] == d0.replace(hour=12).timestamp()
    assert h(env["db"], "the deck contractor", NOW)["start"] is None


def test_hints_people_and_hybrid_words(env):
    d, dbm, s = env["db"], env["dbm"], env["s"]
    sam = dbm.new_speaker(d, "Sam Rivers", 1)
    t = datetime.now().timestamp() - 3600
    ids = []
    for i, (txt, spk) in enumerate([("we picked the Marietta contractor for the deck", sam), ("pizza tonight", None),
                                    ("the deck goes in next month", None), ("TV says deck sale", None)]):
        sid, _ = dbm.insert_segment(d, t + i * 10, 2.0, txt, None, "t", 1)
        d.execute("UPDATE segments SET speaker_id=? WHERE id=?", (spk, sid))
        ids.append(sid)
    d.execute("UPDATE segments SET media=1 WHERE id=?", (ids[3],))
    assert s.hints(d, "what did Sam say about the deck")["speaker_ids"] == [sam]
    got, h = s.hybrid(d, "what did Sam say about the deck contractor")
    assert ids[0] in got and ids[2] in got and ids[3] not in got          # media line never retrieved
    assert s.fts_query("what did Sam say?") == '"sam"'
