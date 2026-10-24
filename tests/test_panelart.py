"""B4-18: panel sketches fill empty storyboard slots (stubbed model), PNGs are valid, failures are recorded once."""
import importlib
import json
import struct
import types
import zlib

import numpy as np
import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAMBLR_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("RAMBLR_PANEL_MODEL", str(tmp_path))          # any existing dir counts as "model present"
    from ramblr import config, db, panelart
    importlib.reload(config)
    for m in (db, panelart):
        importlib.reload(m)
    d = db.connect(config.DB_PATH)
    iid = d.execute("INSERT INTO ideas(key,title,status,source,open,created,updated,last_line_ts) VALUES('k','t','new','trigger',0,0,0,0)").lastrowid
    panels = [{"n": i, "caption": f"cap {i}", "scene": f"scene {i}", "image": None} for i in range(1, 5)]
    panels[0]["image"] = "/already/there.png"
    sid = d.execute("INSERT INTO storyboards(idea_id,version,status,created,body) VALUES(?,1,'ok',0,?)",
                    (iid, json.dumps({"title": "t", "panels": panels}))).lastrowid
    return {"db": d, "pa": panelart, "sid": sid}


class StubPipe:
    def __init__(self):
        self.prompts = []

    def generate(self, prompt, **k):
        self.prompts.append(prompt)
        return types.SimpleNamespace(data=np.full((1, 8, 6, 3), 200, dtype=np.uint8))


def test_png_bytes_roundtrip(env):
    arr = np.arange(4 * 5 * 3, dtype=np.uint8).reshape(4, 5, 3)
    b = env["pa"].png_bytes(arr)
    assert b[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", b[16:24])
    assert (w, h) == (5, 4)
    idat = b[b.index(b"IDAT") + 4:b.index(b"IEND") - 8]
    raw = zlib.decompress(idat)
    assert raw[0] == 0 and raw[1:16] == arr[0].tobytes()


def test_draw_fills_only_empty_slots(env):
    d, pa, sid = env["db"], env["pa"], env["sid"]
    assert pa.pending(d) == sid
    stub = StubPipe()
    out = pa.draw(d, sid, pipe_factory=lambda: (stub, "NPU"))
    assert out["drawn"] == 3 and out["device"] == "NPU"
    assert all("pencil storyboard sketch" in p for p in stub.prompts) and "scene 2" in stub.prompts[0]
    b = json.loads(d.execute("SELECT body FROM storyboards WHERE id=?", (sid,)).fetchone()[0])
    assert b["panels"][0]["image"] == "/already/there.png"
    assert [p["image"] for p in b["panels"][1:]] == [f"/api/storyboards/{sid}/panels/{n}.png" for n in (2, 3, 4)]
    assert pa.panel_path(sid, 3).read_bytes()[:4] == b"\x89PNG"
    assert b["art"]["device"] == "NPU"
    assert pa.pending(d) is None
    assert pa.draw(d, sid, pipe_factory=lambda: pytest.fail("no work left"))["drawn"] == 0


def test_fill_runs_child_and_records_failure_once(env, monkeypatch):
    d, pa, sid = env["db"], env["pa"], env["sid"]
    monkeypatch.setattr(pa, "room", lambda: (False, "load 30 over 24 cores"))
    assert pa.fill(d, run=lambda *a, **k: pytest.fail("must not run without room")) is None
    monkeypatch.setattr(pa, "room", lambda: (True, ""))
    calls = []

    def ok_run(cmd, **k):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout='log line\n{"id": %d, "drawn": 3}\n' % sid, stderr="")
    assert pa.fill(d, run=ok_run) == {"id": sid, "drawn": 3}
    assert calls[0][-2:] == ["ramblr.panelart", str(sid)]
    bad = lambda cmd, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="Traceback\nRuntimeError: NPU busy")
    r = pa.fill(d, run=bad)
    assert r["id"] == sid and "NPU busy" in r["error"]
    assert "NPU busy" in json.loads(d.execute("SELECT body FROM storyboards WHERE id=?", (sid,)).fetchone()[0])["art_error"]
    assert pa.fill(d, run=lambda *a, **k: pytest.fail("no retry loop")) is None


def test_web_serves_panel_png(env):
    from fastapi.testclient import TestClient
    from ramblr import web
    importlib.reload(web)
    pa, sid = env["pa"], env["sid"]
    pa.panel_path(sid, 2).write_bytes(pa.png_bytes(np.zeros((2, 2, 3), dtype=np.uint8)))
    c = TestClient(web.app)
    r = c.get(f"/api/storyboards/{sid}/panels/2.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
    assert c.get(f"/api/storyboards/{sid}/panels/9.png").status_code == 404
