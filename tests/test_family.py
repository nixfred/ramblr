"""B4-14: while family mode is on, new lines go only to the separate family DB (no clip, no main-DB row)."""
import time

import numpy as np


class FakeASR:
    name, device = "fake", "CPU"
    def transcribe(self, a):
        return "family dinner talk", 1


def test_family_lines_go_to_separate_db(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("RAMBLR_FAMILY_DB", str(tmp_path / "family.db"))
    monkeypatch.setenv("RAMBLR_MEDIA", "0")
    from ramblr import config, db, pipeline
    importlib.reload(config); importlib.reload(db); importlib.reload(pipeline)
    ing = pipeline.Ingest(config.DB_PATH, FakeASR(), spool=True, media=False)
    ing.family = True
    ing.q.put((time.time() - 5, np.zeros(16000, np.float32) + 0.01, True))
    ing.q.put((time.time() - 3, np.zeros(16000, np.float32) + 0.01, False))
    ing.wait_idle()
    main = db.connect(config.DB_PATH)
    fam = db.connect(config.FAMILY_DB)
    assert fam.execute("SELECT COUNT(*), MAX(audio) FROM segments").fetchone()[:] == (1, None)
    assert main.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 1          # only the non-family line
    assert (config.FAMILY_DB.stat().st_mode & 0o777) == 0o600
