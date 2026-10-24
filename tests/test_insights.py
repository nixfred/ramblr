"""B3-8/9/10: facts dedupe and supersede, goals, trackers, talk stats (test DB, model stubbed, no embedder)."""
import json
from datetime import datetime

import pytest


@pytest.fixture
def env(monkeypatch):
    import importlib
    from ramblr import config, db, memory, insights
    for m in (db, memory, insights):
        importlib.reload(m)
    monkeypatch.setattr(memory, "_vec", lambda text: None)       # no NPU in tests: text match only
    d = db.connect(config.DB_PATH)
    return {"db": d, "dbm": db, "m": memory, "i": insights}


def test_facts_dedupe_supersede_and_goals(env):
    d, m = env["db"], env["m"]
    ts = {1: 1.0, 2: 2.0, 3: 3.0}
    obj = {"facts": [{"subject": "Sam", "fact": "Sam's mother visits every Thanksgiving.", "kind": "person", "line_ids": [1]},
                     {"subject": "Fred", "fact": "Fred prefers TypeScript.", "line_ids": [99]}],          # untraceable: dropped
           "goals": [{"owner": "Fred", "goal": "Ship Ramblr to the iPhone", "by": "Friday", "line_ids": [2]}]}
    assert m.store(d, "2026-10-20", obj, ts, "stub") == {"facts_new": 1, "facts_bumped": 0, "goals_new": 1}
    again = {"facts": [{"subject": "sam", "fact": "Sam's mother visits every Thanksgiving", "line_ids": [3]}], "goals": []}
    assert m.store(d, "2026-10-22", again, ts, "stub")["facts_bumped"] == 1
    r = d.execute("SELECT mentions, last_seen, line_ids FROM facts").fetchone()
    assert r["mentions"] == 2 and r["last_seen"] == "2026-10-22" and json.loads(r["line_ids"]) == [1, 3]
    sup = {"facts": [{"subject": "Sam", "fact": "Sam's mother now visits at Christmas instead.",
                      "supersedes": "Sam's mother visits every Thanksgiving", "line_ids": [3]}]}
    m.store(d, "2026-10-23", sup, ts, "stub")
    assert [x[0] for x in d.execute("SELECT status FROM facts ORDER BY id")] == ["superseded", "active"]


def test_trackers_and_talk_stats(env):
    d, i, dbm = env["db"], env["i"], env["dbm"]
    fred = dbm.new_speaker(d, "Fred", 1)
    t = datetime.now().replace(hour=10, minute=0).timestamp()
    for k, txt in enumerate(["the NPU is fast", "NPU again", "lunch time", "TV talks about NPU"]):
        sid, _ = dbm.insert_segment(d, t + k * 5, 4.0, txt, None, "t", 1)
        d.execute("UPDATE segments SET speaker_id=?, media=? WHERE id=?", (fred, 1 if "TV" in txt else 0, sid))
    i.add_tracker(d, "NPU", "npu")
    tr = i.trackers(d, 7)
    assert tr[0]["total"] == 2 and tr[0]["series"][-1]["n"] == 2                  # the TV line is not counted
    st = i.talk_stats(d, 7)
    assert st["people"][0]["name"] == "Fred" and st["people"][0]["lines"] == 3 and st["hours"][10] > 0
    with pytest.raises(ValueError):
        i.add_tracker(d, "", "")
