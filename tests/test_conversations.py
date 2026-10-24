"""B3-7: conversations split on silence and on the people changing out; TV and uploads excluded; cards go to the
action-item list; short exchanges get no card. Model calls are stubbed (test DB only)."""
import json
from datetime import datetime

import pytest


@pytest.fixture
def env(monkeypatch):
    import importlib
    from ramblr import config, db, conversations, nightly, llm
    for m in (db, nightly, conversations):
        importlib.reload(m)
    calls = []

    def fake(prompt, **kw):
        calls.append(prompt)
        if "rolling live summary" in prompt:
            return json.dumps({"headline": "deck talk", "bullets": ["a", "b"], "open": []}), "test", "stub", 0.0, ""
        ids = [int(x) for x in __import__("re").findall(r"#(\d+) \[", prompt)]
        assert "family logistics" in prompt and "{templates}" not in prompt
        return json.dumps({"context": "work meeting", "sections": {"decisions": ["Marietta"], "bogus": ["x"]},
                           "title": "Deck plan", "summary": "They chose a contractor.", "decisions": ["Marietta"],
                           "action_items": [{"owner": "Fred", "task": "Email the measurements", "line_ids": ids[:1]}],
                           "people": ["Fred"]}), "test", "stub", 0.0, ""
    monkeypatch.setattr(conversations.llm, "generate", fake)
    return {"db": db.connect(config.DB_PATH), "dbm": db, "c": conversations, "calls": calls}


T = datetime(2026, 10, 23, 9, 0).timestamp()


def say(env, t, spk, text="words", media=0, source=None):
    sid, _ = env["dbm"].insert_segment(env["db"], t, 3.0, text, None, "t", 1, source=source)
    env["db"].execute("UPDATE segments SET speaker_id=?, media=? WHERE id=?", (spk, media, sid))
    return sid


def test_split_on_gap_turnover_and_exclusions(env):
    d, c = env["db"], env["c"]
    fred, sam, tom = (env["dbm"].new_speaker(d, n, 1) for n in ("Fred", "Sam", "Tom"))
    for i in range(5):
        say(env, T + i * 30, fred if i % 2 else sam)
    say(env, T + 100, None, media=1)                 # TV: never part of a conversation
    say(env, T + 110, fred, source="iPhone")         # an upload: not a room conversation
    for i in range(4):                               # 20 min later: a new conversation (silence)
        say(env, T + 1500 + i * 30, fred)
    for i in range(4):                               # only Tom, 3 min later: the people changed out
        say(env, T + 1500 + 90 + 180 + i * 30, tom)
    c.update(d, now=T + 4000, summarize=False)
    rows = d.execute("SELECT lines, status FROM conversations ORDER BY ts0").fetchall()
    assert [r["lines"] for r in rows] == [5, 4, 4] and all(r["status"] == "closed" for r in rows)


def test_card_live_and_short(env):
    d, c = env["db"], env["c"]
    fred = env["dbm"].new_speaker(d, "Fred", 1)
    for i in range(5):
        say(env, T + i * 30, fred, text=f"line {i}")
    c.update(d, now=T + 200)                          # still open: live summary
    assert json.loads(env["dbm"].get_meta(d, "live_summary"))["headline"] == "deck talk"
    c.update(d, now=T + 2000)                         # closed: card written, its action item stored
    card = json.loads(d.execute("SELECT card FROM conversations").fetchone()[0])
    assert card["title"] == "Deck plan" and d.execute("SELECT COUNT(*) FROM action_items").fetchone()[0] == 1
    assert card["context"] == "work meeting" and list(card["sections"]) == ["decisions", "next steps", "risks"]   # template order, bogus dropped
    say(env, T + 5000, fred); say(env, T + 5030, fred)
    c.update(d, now=T + 9000)                         # 2 lines: closed with no card, no model call
    assert d.execute("SELECT card FROM conversations ORDER BY id DESC").fetchone()[0] == "{}"
    n = len(env["calls"])
    c.update(d, now=T + 9100)
    assert len(env["calls"]) == n                     # nothing re-summarised
