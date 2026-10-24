"""B3-12: events are recorded at their sources; webhooks stay off without webhooks.json and sign when on;
plugins parse, schedule once per slot, and store their output (test DB, model and network stubbed)."""
import hashlib
import hmac
import json
from datetime import datetime

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("RAMBLR_CONFIG", str(tmp_path / "cfg"))
    from ramblr import config, db, events, plugins, storyboard, ideas
    importlib.reload(config)
    for m in (db, events, plugins, ideas, storyboard):
        importlib.reload(m)
    (tmp_path / "cfg").mkdir()
    return {"db": db.connect(config.DB_PATH), "dbm": db, "e": events, "p": plugins, "sb": storyboard, "ideas": ideas, "cfg": tmp_path / "cfg"}


def test_storyboard_success_emits_event_and_stays_ok(env, monkeypatch):
    d, sb = env["db"], env["sb"]
    sid, _ = env["dbm"].insert_segment(d, 1792760000, 3.0, "Larry, I have an idea for a dog door", None, "t", 1)
    iid, _ = env["ideas"].create(d, [sid], "range")
    body = {"title": "Dog door", "pitch": "p", "problem": {"text": "t", "for_whom": "f"}, "vision": "v",
            "panels": [{"caption": f"c{i}", "scene": "s"} for i in range(4)], "features": {"mvp": ["a"], "later": []},
            "build": {"machine": "gus", "who": "Larry", "effort": "1d", "stack": "ts"}, "risks": [], "open_questions": [],
            "quotes": [{"ts": "00:00:00", "text": "Larry, I have an idea for a dog door"}], "first_steps": ["a", "b", "c"],
            "score": {"value": 7, "verdict": "worth doing", "why": "x"}}
    monkeypatch.setattr(sb, "gen_claude", lambda prompt, **kw: (json.dumps(body), "stub"))
    monkeypatch.setattr(sb, "generate_mockups", lambda *a, **k: [], raising=False)
    s = sb.build(d, iid, gen="claude")
    assert d.execute("SELECT status FROM storyboards WHERE id=?", (s,)).fetchone()[0] == "ok"
    kinds = [r[0] for r in d.execute("SELECT kind FROM events ORDER BY id")]
    assert kinds == ["idea.new", "idea.storyboard"]


def test_webhooks_off_then_signed(env, monkeypatch):
    d, e = env["db"], env["e"]
    e.emit(d, "upload.done", {"upload_id": 1})
    assert e.dispatch(d) == {"hooks": 0}                       # no webhooks.json: nothing leaves
    (env["cfg"] / "webhooks.json").write_text(json.dumps([{"url": "https://example.test/hook", "events": ["upload.done"], "secret_env": "HK"}]))
    monkeypatch.setenv("HK", "s3cret")
    seen = []

    class R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(req, timeout=10):
        seen.append(req)
        return R()
    monkeypatch.setattr(e.urllib.request, "urlopen", fake)
    out = e.dispatch(d)
    assert out["sent"] == 1 and len(seen) == 1
    req = seen[0]
    assert req.headers["X-ramblr-signature"] == "sha256=" + hmac.new(b"s3cret", req.data, hashlib.sha256).hexdigest()
    assert e.dispatch(d)["sent"] == 0                          # delivered once


def test_plugin_parse_slot_and_run(env, monkeypatch):
    d, p = env["db"], env["p"]
    pd = env["cfg"] / "plugins"; pd.mkdir()
    (pd / "standup.md").write_text("---\nname: Standup\nschedule: daily 08:00\ninput: today\n---\nWrite my standup.\n")
    (pd / "bad.md").write_text("no frontmatter")
    plugins, errors = p.load_all()
    assert [x["name"] for x in plugins] == ["Standup"] and errors[0]["file"] == "bad.md"
    now = datetime.now().replace(hour=9, minute=0).timestamp()
    assert p.slot(plugins[0], datetime.now().replace(hour=7, minute=0).timestamp()) is None
    env["dbm"].insert_segment(d, now - 600, 3.0, "I finished the parser", None, "t", 1)
    monkeypatch.setattr(p.llm, "generate", lambda prompt, **kw: ("Done: parser", "test", "stub", 0, ""))
    assert p.tick(d, now=now) == ["standup"] and p.tick(d, now=now + 60) == []      # once per slot
    assert d.execute("SELECT status, output FROM plugin_runs").fetchone()[:] == ("ok", "Done: parser")
