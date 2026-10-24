"""Fred 2026-10-24 4:38 AM: tests and smoke checks use a separate test DB, never the live one."""
import importlib

import pytest

from conftest import LIVE_DB


def test_connect_refuses_live_db():
    from ramblr import db as dbm
    with pytest.raises(RuntimeError):
        dbm.connect(LIVE_DB)


def test_env_pointing_at_live_db_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("RAMBLR_DB", str(LIVE_DB))
    from ramblr import config, db as dbm
    importlib.reload(config)
    with pytest.raises(RuntimeError):
        dbm.connect()
    monkeypatch.setenv("RAMBLR_DB", str(tmp_path / "test.db"))   # back to a tmp DB (undo() would drop the fixture's too)
    importlib.reload(config)


def test_default_is_a_tmp_db(tmp_path):
    from ramblr import config
    assert str(config.DB_PATH).startswith(str(tmp_path))
