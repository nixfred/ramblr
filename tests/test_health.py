"""B2-5: storage growth comes from real daily DB-size snapshots, never a per-line guess."""
import json
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAMBLR_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("RAMBLR_DATA", str(tmp_path))
    monkeypatch.setenv("RAMBLR_SPOOL", str(tmp_path / "spool"))
    import importlib
    from ramblr import config
    importlib.reload(config)
    from ramblr import db, health
    importlib.reload(db); importlib.reload(health)
    return {"db": db.connect(), "dbm": db, "h": health}


def test_growth_measuring_then_real(env):
    d, h = env["db"], env["h"]
    s = h.storage(d)
    assert s["week_bytes"] is None and s["growth_basis"].startswith("measuring")
    today = datetime.now().strftime("%Y-%m-%d")
    wk = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    size = s["files"]["db"] + s["files"]["db-wal"]
    env["dbm"].set_meta(d, "db_size_log", json.dumps({wk: size - 70000, today: size}))
    s = h.storage(d)
    # the WAL grows a little from the snapshot write itself, so allow that on top of the 70 kB we planted
    assert 70000 <= s["week_bytes"] < 70000 + 512 * 1024 and wk in s["growth_basis"] and s["years_of_disk_at_this_rate"] > 0


def test_report_shape(env):
    r = env["h"].report(env["db"])
    assert {"mic", "npu", "storage", "services", "nightly", "backup"} <= set(r)
    assert [x["unit"] for x in r["services"]][0] == "ramblr-capture"


def test_digest_counts_and_excludes_media(tmp_path, monkeypatch, env):
    from datetime import datetime
    import importlib
    from ramblr import web
    importlib.reload(web)
    d = env["db"]
    t = datetime.now().replace(hour=10, minute=0).timestamp()
    a, _ = env["dbm"].insert_segment(d, t, 3.0, "real talk", None, "t", 1)
    b, _ = env["dbm"].insert_segment(d, t + 10, 3.0, "tv talk", None, "t", 1)
    d.execute("UPDATE segments SET media=1 WHERE id=?", (b,))
    web._local.db = d
    r = web.digest()
    assert r["lines"] == 1 and r["days"] == 7 and "media" in r and r["ideas"] == []
