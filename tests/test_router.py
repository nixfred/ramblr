"""B3-14 voice router on gus: key required (401), default-deny classification, held actions only move on Fred's
approve, read-only asks run (upstream stubbed or forwarded), idempotent, never binds a wildcard, and Ramblr's
commands.route talks to a real router over HTTP (temp DBs only)."""
import importlib
import json
import socket
import threading
import time

import pytest

KEY = "router-test-key"


@pytest.fixture
def rt(tmp_path, monkeypatch):
    monkeypatch.setenv("RAMBLR_VOICE_ROUTER_KEY", KEY)
    monkeypatch.delenv("RAMBLR_GROKBOT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("RAMBLR_GROKBOT_WEBHOOK_KEY", raising=False)
    from ramblr import router
    importlib.reload(router)
    from fastapi.testclient import TestClient
    return {"r": router, "c": TestClient(router.make_app(tmp_path / "router.db")), "db": tmp_path / "router.db"}


H = {"Authorization": f"Bearer {KEY}"}


def cmd(cid, task):
    return {"command_id": cid, "target": "Grok Bot", "task": task, "transcript": "Larry, tell Grok Bot to " + task,
            "speaker": "Fred", "speaker_confidence": 0.9}


def test_401_without_key(rt):
    c = rt["c"]
    for path, body in (("/route", cmd("a", "summarize the deck")), ("/approve/a", None)):
        assert c.post(path, json=body).status_code == 401
        assert c.post(path, json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/log").status_code == 401


def test_read_runs_action_held_until_approved(rt):
    c = rt["c"]
    r = c.post("/route", json=cmd("r1", "what did we decide about the deck"), headers=H).json()
    assert r["class"] == "read" and r["status"] == "ready" and "upstream not configured" in r["result"]
    h = c.post("/route", json=cmd("h1", "send Javier the notes"), headers=H).json()
    assert h["class"] == "action" and h["status"] == "held" and "send" in h["why"]
    amb = c.post("/route", json=cmd("h2", "continue working on Ramblr"), headers=H).json()
    assert amb["status"] == "held"                                   # not clearly read-only: held
    assert c.post("/route", json=cmd("h1", "send Javier the notes"), headers=H).json()["status"] == "held"   # idempotent
    a = c.post("/approve/h1", headers=H).json()
    assert a["status"] == "approved" and a["result"].startswith("Fred said yes")
    assert c.post("/approve/r1", headers=H).json()["status"] == "ready"   # approving a non-held command changes nothing
    assert c.post("/approve/nope", headers=H).status_code == 404
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        sqlite3.connect(rt["db"]).execute("DELETE FROM router_log")   # never deleted


def test_upstream_forward_only_read_or_approved(rt, monkeypatch):
    r, c = rt["r"], rt["c"]
    monkeypatch.setenv("RAMBLR_GROKBOT_WEBHOOK_URL", "https://grokbot.invalid/hook")
    monkeypatch.setenv("RAMBLR_GROKBOT_WEBHOOK_KEY", "up-key")
    sent = []

    class Resp:
        status = 202
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(req, timeout=0):
        sent.append((req.full_url, req.headers.get("Authorization"), json.loads(req.data)))
        return Resp()
    monkeypatch.setattr(r.urllib.request, "urlopen", fake)
    assert c.post("/route", json=cmd("f1", "summarize yesterday"), headers=H).json()["status"] == "forwarded"
    assert c.post("/route", json=cmd("f2", "delete the old branch"), headers=H).json()["status"] == "held"
    assert len(sent) == 1 and sent[0][1] == "Bearer up-key" and sent[0][2]["approved_by_fred"] is False
    assert c.post("/approve/f2", headers=H).json()["status"] == "forwarded"
    assert len(sent) == 2 and sent[1][2]["approved_by_fred"] is True and sent[1][2]["task"] == "delete the old branch"


def test_hosts_never_wildcard(rt, monkeypatch):
    r = rt["r"]
    monkeypatch.setenv("RAMBLR_ROUTER_TAILNET_IP", "100.64.0.7")
    assert r.hosts() == ["127.0.0.1", "100.64.0.7"]
    for bad in ("0.0.0.0", "10.0.0.5", "192.168.1.2", ""):
        monkeypatch.setenv("RAMBLR_ROUTER_TAILNET_IP", bad)
        if bad == "":
            monkeypatch.setattr(r.subprocess, "run", lambda *a, **k: type("P", (), {"stdout": ""})())
        with pytest.raises(SystemExit):
            r.hosts()


def test_ramblr_routes_to_real_router_over_http(rt, monkeypatch):
    import uvicorn
    from ramblr import config, db, commands
    importlib.reload(db); importlib.reload(commands)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(rt["r"].make_app(rt["db"]), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    monkeypatch.setenv("RAMBLR_VOICE_ROUTER_URL", f"http://127.0.0.1:{port}/route")
    d = db.connect(config.DB_PATH)
    fred = db.new_speaker(d, "Fred", 1)
    for i, text in enumerate(["Larry, tell Grok Bot to summarize the deck notes", "Larry, tell Grok Bot to post the release on X"]):
        sid, _ = db.insert_segment(d, 1792760000 + i * 20, 3.0, text, None, "t", 1)
        d.execute("UPDATE segments SET media=0, diar=1, speaker_id=?, sim=0.9 WHERE id=?", (fred, sid))
    commands.detect(d, now=1792760200)
    commands.route(d, now=1792760300)
    rows = {r["task"]: dict(r) for r in d.execute("SELECT task, status, result, command_id FROM command_intents")}
    assert rows["summarize the deck notes"]["status"] == "ready"
    held = rows["post the release on X"]
    assert held["status"] == "held" and "needs Fred's yes" in held["result"]
    assert commands.approve(d, held["command_id"]) == "approved"
    assert d.execute("SELECT status FROM command_intents WHERE command_id=?", (held["command_id"],)).fetchone()[0] == "approved"
    monkeypatch.setattr(commands, "router", lambda: (f"http://127.0.0.1:{port}/route", "wrong"))   # client key only
    sid, _ = db.insert_segment(d, 1792761000, 3.0, "Larry, tell Grok Bot to check the logs", None, "t", 1)
    d.execute("UPDATE segments SET media=0, diar=1, speaker_id=?, sim=0.9 WHERE id=?", (fred, sid))
    commands.detect(d, now=1792761200)
    commands.route(d, now=1792761300)
    assert d.execute("SELECT status, result FROM command_intents WHERE segment_id=?", (sid,)).fetchone()[:] == ("retry", "HTTPError")
    srv.should_exit = True
    t.join(5)
