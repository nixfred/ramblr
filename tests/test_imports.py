"""B4-1: Zoom VTT and Wispr exports land on the room timeline by time, tagged, once; they never start ideas,
route commands or count as people in the room (test DB only)."""
import json
from datetime import datetime

import pytest

VTT = """WEBVTT

1
00:00:01.000 --> 00:00:04.500
Fred Nix: Larry, I have an idea about a meeting bot.

2
00:01:02.000 --> 00:01:05.000
Dana Lee: Larry, tell vic to email the deck.
"""


@pytest.fixture
def env():
    import importlib
    from ramblr import config, db, imports, ideas, commands, presence
    for m in (db, imports, ideas, commands, presence):
        importlib.reload(m)
    return {"db": db.connect(config.DB_PATH), "i": imports, "ideas": ideas, "cmd": commands, "p": presence}


def test_zoom_vtt_once_and_isolated(env):
    d, i = env["db"], env["i"]
    out = i.import_file(d, "zoom", "zoom_2026-10-23_09-30.vtt", VTT)
    assert out["lines"] == 2 and out["start"] == datetime(2026, 10, 23, 9, 30).timestamp()
    rows = d.execute("SELECT s.ts, s.source, sp.name FROM segments s JOIN speakers sp ON sp.id=s.speaker_id ORDER BY s.ts").fetchall()
    assert [r["name"] for r in rows] == ["Fred Nix", "Dana Lee"] and rows[1]["ts"] == out["start"] + 62 and rows[0]["source"] == "Zoom"
    assert i.import_file(d, "zoom", "again.vtt", VTT)["already"] is True
    assert d.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 2
    env["ideas"].scan(d)
    assert d.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 0           # imported lines never start ideas
    assert env["cmd"].detect(d, now=out["start"] + 1000) == []                 # nor route commands
    a = datetime(2026, 10, 23).timestamp()
    assert env["p"].intervals(d, a, a + 86400) == []                           # nor put Zoom people "in the room"


def test_wispr_json_and_csv(env):
    d, i = env["db"], env["i"]
    js = json.dumps([{"text": "Remember to call the plumber", "timestamp": "2026-10-23T10:00:00", "app": "Notes"}, {"text": ""}])
    assert i.import_file(d, "wispr", "w.json", js)["lines"] == 1
    csv = "text,timestamp\nShip the iPhone shortcut,1792760000\n"
    assert i.import_file(d, "wispr", "w.csv", csv)["lines"] == 1
    assert {r[0] for r in d.execute("SELECT source FROM segments")} == {"Wispr"}
    with pytest.raises(ValueError):
        i.import_file(d, "zoom", "nodate.vtt", VTT)
