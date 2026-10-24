"""B3-1: fixes keep the original, learn word pairs, and correct new lines; nothing is deleted (test DB only)."""
import sqlite3

import pytest


@pytest.fixture
def env():
    import importlib
    from ramblr import config, db, vocab
    importlib.reload(db); importlib.reload(vocab)
    return {"db": db.connect(config.DB_PATH), "dbm": db, "v": vocab}


def test_fix_learns_and_applies(env):
    d, v = env["db"], env["v"]
    sid, _ = env["dbm"].insert_segment(d, 1792760000, 2.0, "I installed rambler on oh march e today.", None, "t", 1)
    out = v.correct_line(d, sid, "I installed Ramblr on Omarchy today.")
    assert out["changed"] and ["rambler", "Ramblr"] in out["learned"] and ["oh march e", "Omarchy"] in out["learned"]
    r = d.execute("SELECT text, text_orig FROM segments WHERE id=?", (sid,)).fetchone()
    assert r["text_orig"].startswith("I installed rambler") and r["text"] == "I installed Ramblr on Omarchy today."
    text, changed = v.apply(d, "Rambler runs on oh march e. Ramblers too.")
    assert changed and text == "Ramblr runs on Omarchy. Ramblers too."       # whole words only
    v.correct_line(d, sid, "I installed Ramblr on Omarchy, today.")
    assert d.execute("SELECT text_orig FROM segments WHERE id=?", (sid,)).fetchone()[0].startswith("I installed rambler")
    assert d.execute("SELECT COUNT(*) FROM text_edits").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError):
        d.execute("DELETE FROM vocab")


def test_manual_word_and_switch_off(env):
    d, v = env["db"], env["v"]
    v.add_word(d, "Larry", wrong="lorry")
    assert v.apply(d, "ask lorry")[0] == "ask Larry"
    vid = d.execute("SELECT id FROM vocab").fetchone()[0]
    v.set_active(d, vid, False)
    assert v.apply(d, "ask lorry")[0] == "ask lorry"
    assert "Larry" not in v.prompt_words(d)                 # switched-off words leave the prompt too
