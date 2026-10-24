"""B3-2: the MCP tools read through a connection that cannot write, ATTACH or change PRAGMAs (test DB only)."""
import sqlite3

import pytest


@pytest.fixture
def env():
    import importlib
    from ramblr import config, db, mcp_server
    importlib.reload(db); importlib.reload(mcp_server)
    d = db.connect(config.DB_PATH)
    fred = db.new_speaker(d, "Fred", 1)
    for i, t in enumerate(["we picked the Marietta contractor for the deck", "the TV said deck sale", "pizza tonight"]):
        sid, _ = db.insert_segment(d, 1792760000 + i * 10, 2.0, t, None, "t", 1)
        d.execute("UPDATE segments SET speaker_id=?, media=? WHERE id=?", (fred, 1 if "TV" in t else 0, sid))
    return {"t": mcp_server.Tools(config.DB_PATH), "m": mcp_server, "path": config.DB_PATH}


def test_tools_read(env):
    t = env["t"]
    hits = t.search("deck contractor")["lines"]
    assert [h["text"] for h in hits] == ["we picked the Marietta contractor for the deck"]      # TV line excluded
    day = hits[0]["day"]
    assert len(t.lines(day=day)["lines"]) == 2 and len(t.lines(day=day, include_media=True)["lines"]) == 3
    assert t.speakers()["speakers"][0]["name"] == "Fred"
    with pytest.raises(ValueError):
        t.lines()
    with pytest.raises(ValueError):
        t.ideas(status="'; DROP TABLE segments; --")


@pytest.mark.parametrize("sql", ["INSERT INTO meta(key,value) VALUES('x','y')", "UPDATE segments SET text='x'",
                                 "DELETE FROM meta", "CREATE TABLE evil(x)", "ATTACH DATABASE ':memory:' AS evil",
                                 "PRAGMA writable_schema=1", "DROP TABLE segments"])
def test_connection_refuses_writes(env, sql):
    d = env["m"].ro_connect(env["path"])
    with pytest.raises(sqlite3.DatabaseError):
        d.execute(sql)
