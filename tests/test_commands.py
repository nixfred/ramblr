"""B3-14: "Larry, tell <bot> to <task>" is parsed, deduped, never taken from TV lines, stubbed without a router,
and posted with the payload when one is set (test DB, network stubbed)."""
import json

import pytest


@pytest.fixture
def env(monkeypatch):
    import importlib
    from ramblr import config, db, commands
    importlib.reload(db); importlib.reload(commands)
    monkeypatch.delenv("RAMBLR_VOICE_ROUTER_URL", raising=False)
    monkeypatch.delenv("RAMBLR_VOICE_ROUTER_KEY", raising=False)
    return {"db": db.connect(config.DB_PATH), "dbm": db, "c": commands}


def line(env, ts, text, media=0, spk=None):
    sid, _ = env["dbm"].insert_segment(env["db"], ts, 3.0, text, None, "t", 1)
    env["db"].execute("UPDATE segments SET media=?, diar=1, speaker_id=?, sim=0.91 WHERE id=?", (media, spk, sid))
    return sid


def test_parse():
    from ramblr import commands as c
    assert c.parse("Larry, tell Grok Bot to summarize the deck notes.") == ("Grok Bot", "summarize the deck notes.")
    assert c.parse("larry tell vic to rebuild the plugin") == ("vic", "rebuild the plugin")
    assert c.parse("I told Larry about it") is None


def test_detect_dedupe_tv_and_stub(env):
    d, c = env["db"], env["c"]
    fred = env["dbm"].new_speaker(d, "Fred", 1)
    line(env, 1792760000, "Larry, tell Grok Bot to draft the release notes for Ramblr", spk=fred)
    line(env, 1792760010, "Larry, tell Grok Bot to buy a boat", media=1)             # the TV
    new = c.detect(d, now=1792760100)
    assert len(new) == 1 and c.detect(d, now=1792760200) == []
    c.route(d, now=1792760300)
    r = d.execute("SELECT target, task, speaker, confidence, status FROM command_intents").fetchone()
    assert r[:] == ("Grok Bot", "draft the release notes for Ramblr", "Fred", 0.91, "stub")


def test_route_posts_payload(env, monkeypatch):
    d, c = env["db"], env["c"]
    fred = env["dbm"].new_speaker(d, "Fred", 1)
    line(env, 1792760000, "Larry, tell vic to rebuild the bar plugin tonight", spk=fred)
    c.detect(d, now=1792760100)
    monkeypatch.setenv("RAMBLR_VOICE_ROUTER_URL", "https://router.example/hook")
    monkeypatch.setenv("RAMBLR_VOICE_ROUTER_KEY", "k")
    sent = []

    class R:
        status = 200
        def read(self, n): return b'{"ok":true}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(c.urllib.request, "urlopen", lambda req, timeout=10: (sent.append(req), R())[1])
    assert c.route(d, now=1792760300) == 1
    body = json.loads(sent[0].data)
    assert body["target"] == "vic" and body["task"] == "rebuild the bar plugin tonight" and body["command_id"]
    assert sent[0].headers["Authorization"] == "Bearer k" and sent[0].headers["Idempotency-key"] == body["command_id"]
    assert d.execute("SELECT status FROM command_intents").fetchone()[0] == "sent"
    assert c.route(d, now=1792760400) == 0                                           # never routed twice


def test_only_freds_voice_routes(env):
    d, c = env["db"], env["c"]
    fred = env["dbm"].new_speaker(d, "Fred Nix", 1)
    sam = env["dbm"].new_speaker(d, "Sam", 1)
    d.execute("UPDATE speakers SET confirmed=1 WHERE id=?", (fred,))
    line(env, 1792760000, "Larry, tell vic to delete everything", spk=sam)
    line(env, 1792760010, "Larry, tell vic to rebuild the plugin", spk=fred)
    assert len(c.detect(d, now=1792760100)) == 1
    st = {r[0]: r[1] for r in d.execute("SELECT task, status FROM command_intents")}
    assert st == {"delete everything": "ignored", "rebuild the plugin": "queued"}


def test_idea_trigger_waits_for_voice_then_gates(env):
    import importlib
    from ramblr import ideas
    importlib.reload(ideas)
    d = env["db"]
    fred = env["dbm"].new_speaker(d, "Fred", 1); sam = env["dbm"].new_speaker(d, "Sam", 1)
    sid, _ = env["dbm"].insert_segment(d, 1792760000, 3.0, "Larry, I have an idea for a bike rack", None, "t", 1)
    d.execute("UPDATE segments SET diar=0 WHERE id=?", (sid,))
    ideas.scan(d, now=1792760010)                          # voice not known yet: waits
    assert d.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 0
    d.execute("UPDATE segments SET diar=1, speaker_id=? WHERE id=?", (sam, sid))
    ideas.scan(d, now=1792760020)                          # Sam said it: no idea
    assert d.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 0
    sid2, _ = env["dbm"].insert_segment(d, 1792760100, 3.0, "Larry, I have an idea for a shed", None, "t", 1)
    d.execute("UPDATE segments SET diar=1, speaker_id=? WHERE id=?", (fred, sid2))
    ideas.scan(d, now=1792760110)
    assert d.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 1
