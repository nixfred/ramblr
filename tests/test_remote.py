"""B4-19: extra mics stream PCM over the tailnet; the web checks the key and relays to ramblr-mics, which runs the
same VAD -> Whisper -> SQLite path (stub Whisper here) with lines tagged mic:<device>. Mute drops remote audio."""
import importlib
import socket
import threading
from pathlib import Path

import numpy as np
import pytest

JFK = Path.home() / ".local/share/ramblr/test/jfk16.wav"


class StubASR:
    name, device = "stub", "CPU"

    def __init__(self):
        self.calls = 0

    def transcribe(self, samples):
        self.calls += 1
        return f"remote line {self.calls}", 3


@pytest.fixture
def env():
    from ramblr import config, db, pipeline, remote, state
    for m in (db, state, pipeline, remote):
        importlib.reload(m)
    if not JFK.exists() or not config.VAD_MODEL.exists():
        pytest.skip("needs the JFK sample and the Silero model on this box")
    import soundfile as sf
    pcm = (sf.read(str(JFK), dtype="int16")[0]).astype("<i2").tobytes()
    srv = remote.Server(config.MICS_SOCK, remote.Hub(asr_factory=StubASR))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield {"cfg": config, "db": db.connect(config.DB_PATH), "remote": remote, "state": state, "pcm": pcm, "srv": srv}
    srv.shutdown()
    srv.server_close()


def send(cfg, device, pcm, step=3333):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(cfg.MICS_SOCK))
    s.sendall(b'{"device": "%s", "rate": 16000}\n' % device.encode())
    f = s.makefile("rb")
    ack = f.readline().decode().strip()
    if ack == "ok":
        for i in range(0, len(pcm), step):          # odd step: chunks split samples
            s.sendall(pcm[i:i + step])
    s.shutdown(socket.SHUT_WR)
    return s, ack


def wait_done(env, device, conn):
    conn.settimeout(30)
    conn.recv(1)                                     # server closes once the stream is flushed and transcribed
    conn.close()
    for _ in range(200):
        st = env["remote"].status() or {}
        if st.get(device) and not st[device]["connected"]:
            return st[device]
        threading.Event().wait(0.05)
    raise AssertionError("stream never finished")


def test_stream_lands_on_timeline_tagged(env):
    conn, ack = send(env["cfg"], "vic", env["pcm"])
    assert ack == "ok"
    st = wait_done(env, "vic", conn)
    rows = env["db"].execute("SELECT text, source FROM segments ORDER BY id").fetchall()
    assert rows and all(r["source"] == "mic:vic" for r in rows) and rows[0]["text"].startswith("remote line")
    assert st["lines"] == len(rows) and st["bytes"] == len(env["pcm"])
    assert oct(env["cfg"].MICS_SOCK.stat().st_mode & 0o777) == "0o600"


def test_mute_drops_remote_audio(env):
    env["state"].update(by="test", muted=True)
    conn, ack = send(env["cfg"], "pi", env["pcm"])
    assert ack == "ok"
    st = wait_done(env, "pi", conn)
    assert st["lines"] == 0 and env["db"].execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 0


def test_claim_rules(env):
    hub = env["srv"].hub
    assert "bad device" in hub.claim("Vic Laptop")
    assert not isinstance(hub.claim("den"), str)
    assert hub.claim("den") == "device already streaming"
    hub.release("den")
    assert not isinstance(hub.claim("den"), str)


def test_web_auth_and_relay(env, monkeypatch):
    from fastapi.testclient import TestClient
    from ramblr import web
    importlib.reload(web)
    cfg = env["cfg"]
    monkeypatch.setattr(cfg, "MIC_KEY", "test-mic-key")
    c = TestClient(web.app)
    url = "/api/mic/stream?device=office"
    assert c.post(url, content=b"\0" * 64).status_code == 401
    assert c.post(url, content=b"", headers={"Authorization": "Bearer nope"}).status_code == 401
    good = {"Authorization": "Bearer test-mic-key"}
    assert c.post("/api/mic/stream?device=Bad!", content=b"", headers=good).status_code == 400
    r = c.post(url, content=env["pcm"], headers=good)
    assert r.status_code == 200 and r.json()["seconds"] == round(len(env["pcm"]) / 32000, 1)
    assert env["db"].execute("SELECT COUNT(*) FROM segments WHERE source='mic:office'").fetchone()[0] > 0
    assert c.get("/api/mics/remote").json()["devices"]["office"]["connected"] is False
    env["srv"].shutdown(); env["srv"].server_close(); cfg.MICS_SOCK.unlink()
    assert c.post(url, content=b"", headers=good).status_code == 503
    assert c.get("/api/mics/remote").json()["service"] == "not running"
