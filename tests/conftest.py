"""Every test runs against a throwaway DB, never the live one (Fred, 2026-10-24 4:38 AM ET).

- Before each test, RAMBLR_DB / RAMBLR_STATE / RAMBLR_SPOOL / sockets (notify, ideas, mics) point into the test's tmp dir unless the test
  sets its own (still a tmp path), and ramblr.config is reloaded so module defaults follow.
- After setup, the test FAILS if the configured DB is the live path.
- ramblr.db.connect() itself refuses the live path while pytest is running (belt and braces)."""
import importlib
import os
from pathlib import Path

import pytest

LIVE_DB = (Path.home() / ".local/share/ramblr/ears.db").resolve()
LIVE_STATE = (Path.home() / ".config/ramblr/state.json").resolve()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for var, val in (("RAMBLR_DB", tmp_path / "test.db"), ("RAMBLR_STATE", tmp_path / "state.json"),
                     ("RAMBLR_SPOOL", tmp_path / "spool"), ("RAMBLR_SOCK", tmp_path / "n.sock"),
                     ("RAMBLR_IDEAS_SOCK", tmp_path / "i.sock"), ("RAMBLR_MICS_SOCK", tmp_path / "m.sock"),
                     ("RAMBLR_ASR_FALLBACK_FILE", tmp_path / "asr-fallback.json"), ("RAMBLR_WATCHDOG_FILE", tmp_path / "watchdog.json")):
        if not os.environ.get(var) or Path(os.environ[var]).resolve() in (LIVE_DB, LIVE_STATE):
            monkeypatch.setenv(var, str(val))
    from ramblr import config
    importlib.reload(config)
    yield
    from ramblr import config as cfg
    assert Path(cfg.DB_PATH).resolve() != LIVE_DB, "a test pointed ramblr at the live DB"
    assert Path(cfg.STATE_FILE).resolve() != LIVE_STATE, "a test pointed ramblr at the live state.json"

