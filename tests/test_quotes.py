"""B4-11: only verbatim quotes survive; markdown export (test DB, model stubbed)."""
import json
import time


def test_pull_verbatim_only(monkeypatch):
    import importlib
    from ramblr import config, db, quotes
    importlib.reload(db); importlib.reload(quotes)
    d = db.connect(config.DB_PATH)
    t = time.time() - 300
    a, _ = db.insert_segment(d, t, 3, "Honestly the NPU is the most underrated chip in this laptop.", None, "t", 1)
    b, _ = db.insert_segment(d, t + 10, 3, "Anyway, lunch.", None, "t", 1)
    monkeypatch.setattr(quotes.llm, "generate", lambda p, **k: (json.dumps({"title": "NPU", "quotes": [
        {"line_id": a, "quote": "the NPU is the most underrated chip in this laptop", "why": "punchy"},
        {"line_id": b, "quote": "lunch is the best meal", "why": "made up"}]}), "test", "stub", 0, ""))
    r = quotes.pull(d, t - 1, t + 20)
    assert [q["line_id"] for q in r["quotes"]] == [a] and r["dropped"] == 1
    md = quotes.markdown(d, r["id"])
    assert "> the NPU is the most underrated chip in this laptop" in md and "punchy" in md
