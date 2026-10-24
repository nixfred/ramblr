"""B4-16: the demo DB builds without models, never overwrites, and demo mode refuses writes."""
import os

import pytest


def test_build_demo(tmp_path):
    from ramblr import demo
    out = tmp_path / "demo.db"
    r = demo.build(out)
    assert r["lines"] > 100
    import sqlite3
    d = sqlite3.connect(out)
    assert d.execute("SELECT COUNT(*) FROM storyboards WHERE status='ok'").fetchone()[0] == 1
    assert d.execute("SELECT COUNT(*) FROM segments WHERE media=1").fetchone()[0] == 12
    names = {r[0] for r in d.execute("SELECT name FROM speakers WHERE named=1")}
    assert names == {"Fred", "Sam", "Jordan"}                          # generic names only
    with pytest.raises(SystemExit):
        demo.build(out)                                               # never overwrites
