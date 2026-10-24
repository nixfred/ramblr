"""B3-11: auto placements never override a manual move; folders are made once, case-insensitively."""
import pytest


@pytest.fixture
def env():
    import importlib
    from ramblr import config, db, folders
    importlib.reload(db); importlib.reload(folders)
    return {"db": db.connect(config.DB_PATH), "f": folders}


def test_place_manual_wins(env):
    d, f = env["db"], env["f"]
    assert f.place(d, "idea", 1, "Ramblr")
    assert f.place(d, "idea", 1, "ramblr ") and f.names(d) == ["Ramblr"]          # same folder, any case
    f.place(d, "idea", 1, "House", source="manual")
    assert not f.place(d, "idea", 1, "Work")                                       # auto never moves a manual placement
    fid = d.execute("SELECT folder_id FROM folder_items WHERE item_type='idea' AND item_id=1").fetchone()[0]
    assert d.execute("SELECT name FROM folders WHERE id=?", (fid,)).fetchone()[0] == "House"
    assert not f.place(d, "idea", 2, "  ")
