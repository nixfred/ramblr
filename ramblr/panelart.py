"""Storyboard panel art (B4-18): one black-and-white pencil sketch per panel, made on this box.

Model: LCM Dreamshaper v7 int8 for OpenVINO (MIT, ~1 GB in DATA/models), 4 steps at 384x384. It runs on the Intel
NPU (first compile ~45 s, then cached in PANEL_CACHE; ~2 s per sketch) and falls back to the CPU at nice 19 with 4
threads (~11 s per sketch). Never the NVIDIA GPU: the course bot owns it.

The ideas worker calls fill() each pass. It picks at most one finished storyboard with empty image slots, and only
when the box has room (Law 22: 1-minute load under the core count, at least 3 GB available). The drawing itself
runs in a child process (`python -m ramblr.panelart <storyboard id>`) so the model leaves memory when it is done
and the worker never blocks on a compile. PNGs go to DATA/panels/<storyboard id>-<n>.png and the panel's "image"
field gets the URL that serves it. A failure is recorded on the storyboard (art_error) and not retried in a loop."""
import json
import logging
import os
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

from . import config

log = logging.getLogger("ramblr.panelart")
STEPS = 4
TIMEOUT = 900
STYLE = "black and white pencil storyboard sketch, {scene}, clean line art, white background"


def panel_dir():
    p = config.DATA / "panels"
    p.mkdir(parents=True, exist_ok=True)
    return p


def panel_path(storyboard_id, n):
    return panel_dir() / f"{storyboard_id}-{n}.png"


def panel_url(storyboard_id, n):
    return f"/api/storyboards/{storyboard_id}/panels/{n}.png"


def png_bytes(arr):
    """uint8 HxWx3 array to PNG bytes with the stdlib (no Pillow in the venv)."""
    h, w, c = arr.shape
    if c != 3:
        raise ValueError("need RGB")
    raw = b"".join(b"\x00" + arr[y].tobytes() for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def prompt(panel):
    scene = " ".join(f"{panel.get('caption', '')}. {panel.get('scene', '')}".split())[:220]
    return STYLE.format(scene=scene)


def room():
    """Law 22: (ok, why)."""
    try:
        load1 = float(open("/proc/loadavg").read().split()[0])
        avail = next(int(l.split()[1]) for l in open("/proc/meminfo") if l.startswith("MemAvailable:")) / 1048576
    except Exception as e:
        return False, f"cannot read load: {e}"
    if load1 > (os.cpu_count() or 1):
        return False, f"load {load1:.1f} over {os.cpu_count()} cores"
    if avail < 3:
        return False, f"only {avail:.1f} GB available"
    return True, ""


def pending(db):
    """Newest finished storyboard with an empty image slot and no recorded art failure, or None."""
    for r in db.execute("SELECT id, body FROM storyboards WHERE status='ok' AND body IS NOT NULL ORDER BY id DESC LIMIT 50"):
        try:
            b = json.loads(r["body"])
        except ValueError:
            continue
        if b.get("art_error"):
            continue
        if any(not p.get("image") for p in b.get("panels") or []):
            return r["id"]
    return None


def _pipeline():
    import openvino_genai as ov_genai
    pipe = ov_genai.Text2ImagePipeline(str(config.PANEL_MODEL))
    pipe.reshape(1, config.PANEL_SIZE, config.PANEL_SIZE, 1.0)
    config.PANEL_CACHE.mkdir(parents=True, exist_ok=True)
    if config.PANEL_DEVICE != "CPU":
        try:
            pipe.compile(config.PANEL_DEVICE, config={"CACHE_DIR": str(config.PANEL_CACHE)})
            return pipe, config.PANEL_DEVICE
        except Exception as e:
            log.warning("panel art: %s compile failed (%s), using the CPU at nice 19", config.PANEL_DEVICE, type(e).__name__)
            pipe = ov_genai.Text2ImagePipeline(str(config.PANEL_MODEL))
            pipe.reshape(1, config.PANEL_SIZE, config.PANEL_SIZE, 1.0)
    os.nice(19 - os.nice(0))
    pipe.compile("CPU", config={"INFERENCE_NUM_THREADS": 4, "CACHE_DIR": str(config.PANEL_CACHE)})
    return pipe, "CPU"


def draw(db, storyboard_id, pipe_factory=_pipeline):
    """Fill every empty slot of one storyboard. Runs in the child process. Returns a summary dict."""
    r = db.execute("SELECT body FROM storyboards WHERE id=? AND status='ok'", (storyboard_id,)).fetchone()
    if not r:
        raise LookupError("no such finished storyboard")
    todo = [p for p in json.loads(r["body"]).get("panels") or [] if not p.get("image")]
    if not todo:
        return {"id": storyboard_id, "drawn": 0}
    t0 = time.time()
    pipe, device = pipe_factory()
    made = {}
    for p in todo:
        img = pipe.generate(prompt(p), num_inference_steps=STEPS, rng_seed=1000 * storyboard_id + p["n"])
        arr = img.data[0] if hasattr(img, "data") else img[0]
        path = panel_path(storyboard_id, p["n"])
        tmp = path.with_suffix(".part")
        tmp.write_bytes(png_bytes(arr))
        tmp.replace(path)
        made[p["n"]] = panel_url(storyboard_id, p["n"])
    secs = round(time.time() - t0, 1)
    db.execute("BEGIN IMMEDIATE")              # re-read: another writer may have touched the body meanwhile
    try:
        b = json.loads(db.execute("SELECT body FROM storyboards WHERE id=?", (storyboard_id,)).fetchone()["body"])
        for p in b.get("panels") or []:
            if p["n"] in made and not p.get("image"):
                p["image"] = made[p["n"]]
        b["art"] = {"device": device, "model": config.PANEL_MODEL.name, "steps": STEPS, "seconds": secs}
        db.execute("UPDATE storyboards SET body=? WHERE id=?", (json.dumps(b), storyboard_id))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return {"id": storyboard_id, "drawn": len(made), "device": device, "seconds": secs}


def _record_error(db, storyboard_id, msg):
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT body FROM storyboards WHERE id=?", (storyboard_id,)).fetchone()
        b = json.loads(row["body"])
        b["art_error"] = msg[:300]
        db.execute("UPDATE storyboards SET body=? WHERE id=?", (json.dumps(b), storyboard_id))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def fill(db, run=subprocess.run):
    """Worker pass: draw one storyboard's missing panels in a child process when there is room. Returns a summary
    dict, or None when there is nothing to do or no room."""
    if not config.PANEL_ART or not config.PANEL_MODEL.is_dir():
        return None
    sid = pending(db)
    if sid is None:
        return None
    ok, why = room()
    if not ok:
        log.info("panel art for storyboard %d deferred: %s", sid, why)
        return None
    try:
        p = run([sys.executable, "-m", "ramblr.panelart", str(sid)], capture_output=True, text=True, timeout=TIMEOUT,
                cwd=str(Path(__file__).resolve().parent.parent))
        if p.returncode != 0:
            tail = (p.stderr or p.stdout or "").strip().splitlines()
            raise RuntimeError(tail[-1] if tail else f"exit {p.returncode}")
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        _record_error(db, sid, msg)
        return {"id": sid, "error": msg}
    from . import notify
    r = db.execute("SELECT idea_id FROM storyboards WHERE id=?", (sid,)).fetchone()
    if r:
        notify.send({"t": "idea", "id": r["idea_id"]})
    return out


if __name__ == "__main__":
    from . import db as dbm
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    print(json.dumps(draw(dbm.connect(config.DB_PATH), int(sys.argv[1]))))
