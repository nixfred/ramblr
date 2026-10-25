"""Tailnet-only site. Binds 127.0.0.1; `tailscale serve` is the only way in. No delete endpoint exists."""
import asyncio
import json
import os
import re
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from . import presence as presmod
from . import config, db as dbm, doit, ideas as ideasmod, media as mediamod, mediaskip, mics as micmod, mockups as mockmod, speakers as spk, state

HERE = Path(__file__).parent
BRAND = HERE.parent / "assets" / "brand"
app = FastAPI(title="ramblr", docs_url=None, redoc_url=None, openapi_url=None)
_local = threading.local()


def db():
    if not hasattr(_local, "db"):      # sync endpoints run in a thread pool: one connection per thread
        _local.db = dbm.connect(config.DB_PATH)
    return _local.db


DEMO = os.environ.get("RAMBLR_DEMO") == "1"


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    if DEMO and request.method not in ("GET", "HEAD") and not request.url.path.startswith("/mcp"):
        return JSONResponse({"error": "demo mode is read-only"}, status_code=403)       # B4-16
    # Blocks a page on another origin from POSTing into this site through the browser.
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("origin")
        host = request.headers.get("host", "")
        if origin and origin.split("://", 1)[-1] != host:
            return JSONResponse({"error": "cross-origin write refused"}, status_code=403)
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def fmt(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def parse_local(s):
    for f in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, f).timestamp()
        except ValueError:
            pass
    raise HTTPException(400, f"bad time {s!r}")


SEG_SQL = """SELECT s.id, s.ts, s.dur, s.day, s.text, s.noise, s.speaker_id, s.manual, s.diar, s.media, s.source, s.text_orig,
                    COALESCE(sp.name, 'Unknown') AS speaker, sp.color AS sp_color, COALESCE(sp.named,0) AS sp_named
             FROM segments s LEFT JOIN speakers sp ON sp.id = s.speaker_id"""


def provisional(d, ts):
    """Speaker of the latest voice-identified line in the last minute, shown with '?' until diarization decides."""
    r = d.execute("""SELECT sp.name FROM segments s JOIN speakers sp ON sp.id=s.speaker_id
                     WHERE s.diar=1 AND s.ts<? AND s.ts>? ORDER BY s.ts DESC LIMIT 1""", (ts, ts - 60)).fetchone()
    return r[0] if r else None


def row(r):
    prov = provisional(db(), r["ts"]) if r["diar"] == 0 else None
    keys = r.keys()
    named = r["sp_named"] if "sp_named" in keys else 0
    return {"provisional": prov, "color": spk.color_for(r["speaker_id"], named, r["sp_color"] if "sp_color" in keys else None),
            "speaker_named": bool(named), "id": r["id"], "ts": r["ts"], "t": fmt(r["ts"]), "day": r["day"], "dur": round(r["dur"], 1),
            "text": r["text"], "noise": bool(r["noise"]), "speaker_id": r["speaker_id"], "speaker": r["speaker"],
            "manual": bool(r["manual"]), "pending": r["diar"] == 0,
            "media": (r["media"] in (1, 2)) if "media" in keys else False,
            "media_manual": (r["media"] in (2, -1)) if "media" in keys else False,
            "quiet": presmod.is_quiet(r["ts"]), "source": r["source"] if "source" in keys else None,
            "playable": config.AUDIO24_ENABLED and time.time() - r["ts"] < 86400 and not r["noise"],
            "edited": bool(r["text_orig"]) if "text_orig" in keys else False}


@app.get("/api/status")
def status():
    d = db()
    hb = dbm.get_meta(d, "capture_heartbeat")
    last = d.execute("SELECT MAX(ts) FROM segments").fetchone()[0]
    return {"rev": int(dbm.get_meta(d, "rev", "0")), "now": time.time(), "today": datetime.now().strftime("%Y-%m-%d"),
            "capture_age": None if hb is None else round(time.time() - float(hb), 1),
            "last_segment_ts": last, "model": dbm.get_meta(d, "asr_model"), "device": dbm.get_meta(d, "asr_device"),
            "segments": d.execute("SELECT COUNT(*) FROM segments").fetchone()[0],
            "pending": d.execute("SELECT COUNT(*) FROM segments WHERE diar=0").fetchone()[0], "demo": DEMO,
            **capture_status(d, hb)}


def capture_status(d, hb):
    st = state.load()
    mode = dbm.get_meta(d, "capture_state", "starting")
    if hb is None or time.time() - float(hb) > 45:
        mode = "down"
    elif st.get("muted") and mode != "muted":
        mode = "muting"
    elif not st.get("muted") and mode == "muted":
        mode = "starting"
    from . import calls
    return {"call": calls.status(d), "family": bool(st.get("family")), "capture_state": mode, "muted": bool(st.get("muted")), "capture_source": dbm.get_meta(d, "capture_source", ""),
            "capture_chosen": st.get("source") or "", "capture_warning": dbm.get_meta(d, "capture_warning", "")}


@app.get("/api/days")
def days():
    d = db()
    rs = d.execute("SELECT day, COUNT(*) n, MIN(ts) a, MAX(ts) b FROM segments WHERE noise=0 GROUP BY day ORDER BY day DESC").fetchall()
    summ = {r[0] for r in d.execute("SELECT DISTINCT day FROM summaries")}
    return [{"day": r["day"], "count": r["n"], "first": fmt(r["a"]), "last": fmt(r["b"]), "summary": r["day"] in summ} for r in rs]


@app.get("/api/speakers")
def speakers():
    rs = db().execute("""SELECT sp.id, sp.name, sp.named, sp.color, sp.confirmed, COUNT(s.id) n, COALESCE(vp.n,0) prints
                         FROM speakers sp LEFT JOIN segments s ON s.speaker_id=sp.id AND s.noise=0
                         LEFT JOIN voiceprints vp ON vp.speaker_id=sp.id
                         WHERE sp.merged_into IS NULL GROUP BY sp.id ORDER BY sp.named DESC, sp.name""").fetchall()
    return [{"id": r["id"], "name": r["name"], "named": bool(r["named"]), "count": r["n"], "voiceprint_samples": r["prints"],
             "color": spk.color_for(r["id"], r["named"], r["color"]), "custom_color": r["color"], "confirmed": bool(r["confirmed"])}
            for r in rs]


@app.get("/api/segments")
def segments(day: str = "", after_id: int = 0, q: str = "", start: str = "", end: str = "", noise: int = 0, limit: int = 20000,
             tail: int = 0, media: int = -1):
    """media: 1 = include TV / music lines, 0 = leave them out, -1 (default) = include them for the day view (they fold
    into 'Media skipped' rows) but leave them out of a search (q)."""
    where, args = [], []
    if media == 0 or (media == -1 and q.strip()):
        where.append("COALESCE(s.media,0) NOT IN (1,2)")
    if after_id:
        where.append("s.id > ?"); args.append(after_id)
    if day:
        where.append("s.day = ?"); args.append(day)
    if start:
        where.append("s.ts >= ?"); args.append(parse_local(start))
    if end:
        where.append("s.ts <= ?"); args.append(parse_local(end))
    if q.strip():
        where.append("(s.text LIKE ? ESCAPE '\\' OR sp.name LIKE ? ESCAPE '\\')")
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        args += [like, like]
    if not noise:
        where.append("s.noise = 0")
    if tail:     # the newest N lines (the live ticker), returned oldest first
        sql = SEG_SQL + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY s.ts DESC, s.id DESC LIMIT ?"
        return [row(r) for r in reversed(db().execute(sql, args + [max(1, min(tail, 200))]).fetchall())]
    sql = SEG_SQL + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY s.ts, s.id LIMIT ?"
    rs = db().execute(sql, args + [max(1, min(limit, 50000))]).fetchall()
    return [row(r) for r in rs]


@app.get("/api/export", response_class=PlainTextResponse)
def export(start: str, end: str, noise: int = 0, media: int = 0):
    rs = segments(start=start, end=end, noise=noise, media=media)      # media lines are skipped unless media=1
    return "\n".join(f"[{r['t']}] {r['speaker']}: {r['text']}" for r in rs)


def clean_name(body):
    name = " ".join(str(body.get("name", "")).split())[:60]
    if not name:
        raise HTTPException(400, "name required")
    return name


def who_from(request):
    """Who made a change: the tailnet login tailscale serve forwards, else 'local'."""
    return (request.headers.get("tailscale-user-login") or "local")[:80]


def run_op(fn):
    try:
        out = fn()
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    hub.publish("reload", {})
    return {"ok": True, **out}


@app.post("/api/speakers/{sid}/rename")
async def rename(sid: int, request: Request):
    name = clean_name(await request.json())
    return run_op(lambda: spk.rename(db(), sid, name, who_from(request)))


@app.post("/api/speakers/{sid}/color")
async def speaker_color(sid: int, request: Request):
    color = (await request.json()).get("color")
    return run_op(lambda: spk.set_color(db(), sid, color, who_from(request)))


@app.post("/api/speakers/{sid}/confirm")
async def speaker_confirm(sid: int, request: Request):
    """'Confirm this is me' - only Fred's click on the site calls this."""
    return run_op(lambda: spk.confirm(db(), sid, who_from(request)))


def _target(body):
    if body.get("speaker_id") is not None:
        return {"speaker_id": int(body["speaker_id"])}
    return {"name": clean_name(body)}


@app.post("/api/segments/{seg_id}/speaker")
async def set_segment_speaker(seg_id: int, request: Request):
    """Reassign one line; re-scores the day and propagates (response carries the real applied/flagged lists)."""
    body = await request.json()
    t = _target(body)
    out = run_op(lambda: spk.reassign(db(), seg_id, who=who_from(request), propagate=body.get("propagate", True), **t))
    p = out.get("propagation")
    if p:
        for k in ("applied", "flagged"):
            for x in p[k]:
                x["t"] = fmt(x["ts"])
    return out


@app.post("/api/segments/reassign")
async def bulk_reassign(request: Request):
    body = await request.json()
    ids = body.get("segment_ids") or []
    if not ids:
        raise HTTPException(400, "no lines selected")
    t = _target(body)
    return run_op(lambda: spk.bulk(db(), ids, who=who_from(request), **t))


@app.post("/api/segments/media")
async def mark_media(request: Request):
    """One click: 'that was the TV/media' ({"media": true}) or the reverse ({"media": false}) for lines or a range.
    Manual flags are final; each click is a training example and re-decides the rest of that day."""
    body = await request.json()
    ids = body.get("segment_ids") or []
    if not ids and body.get("start") and body.get("end"):
        ids = [x["id"] for x in segments(start=body["start"], end=body["end"], noise=0)]
    if not ids:
        raise HTTPException(400, "no lines selected")
    if body.get("media", True):
        out = run_op(lambda: mediamod.correct(db(), ids, True, who_from(request)))
    else:       # NOT MEDIA: talk again, and diarized afresh so the lines get real speaker labels; "undo" undoes it all
        out = run_op(lambda: mediaskip.not_media(db(), ids, who_from(request)))
    if out.get("spread"):
        hub.publish("upd", {"ids": out["spread"]})
    return out


@app.post("/api/media/notmedia/undo")
async def not_media_undo(request: Request):
    """Undo one NOT MEDIA click: media flags, the speakers the lines had (none) and the classifier go back."""
    body = await request.json()
    back = run_op(lambda: {"restored": len(mediaskip.undo_not_media(db(), str(body.get("batch", ""))[:32], who_from(request)))})
    return back


@app.post("/api/media/spread/undo")
async def media_spread_undo(request: Request):
    """Undo one 'marked N more as TV' batch: those lines go back exactly as they were."""
    body = await request.json()
    back = run_op(lambda: mediamod.undo_spread(db(), str(body.get("batch", ""))[:32]))
    hub.publish("upd", {"ids": back})
    return {"ok": True, "restored": len(back)}


@app.get("/api/media/blocks")
def media_blocks(day: str):
    """Runs of media lines for a day with a label from the classifier: [{ids, start, end, label, t}]."""
    d = db()
    rows = [{"id": r["id"], "ts": r["ts"], "dur": r["dur"], "media": r["media"] in (1, 2), "noise": bool(r["noise"]), "source": r["source"]}
            for r in d.execute("SELECT id, ts, dur, media, noise, source FROM segments WHERE day=? ORDER BY ts, id", (day,))]
    src = {r["id"]: r["source"] for r in rows}
    out = []
    for b in mediamod.blocks(rows):
        srcs = [src[i] for i in b["ids"]]
        lab, title = mediamod.label_for(d, b["start"], b["end"], max(set(srcs), key=srcs.count))   # the block's own mic
        out.append({**b, "label": lab, "title": title, "t": f"{fmt12(b['start'])} to {fmt12(b['end'])}"})
    return out


def fmt12(ts):
    return datetime.fromtimestamp(ts).strftime("%-I:%M %p")


@app.get("/api/media/log")
def media_log(day: str = "", days: int = 7):
    """What played and when: classifier sessions (TV / music heard in the room), player sources (what the player
    itself said), and music fingerprints (title once AcoustID can look it up). Newest first."""
    from . import musicid, players
    d = db()
    if day:
        a, b = day_bounds(day)
    else:
        b = time.time()
        a = b - max(1, min(days, 90)) * 86400
    items = []
    for src in [None] + [r[0] for r in d.execute("SELECT DISTINCT source FROM media_windows WHERE source IS NOT NULL")]:
        for s in mediamod.sessions(d, a, b, source=src):
            lab, title = mediamod.label_for(d, s["t0"], s["t1"], src)
            kind = "music" if lab == "Music" else "tv"
            items.append({"kind": kind, "src": src or "room", "t0": s["t0"], "t1": s["t1"],
                          "title": title or ("Music playing" if kind == "music" else "TV playing"),
                          "lines": d.execute("SELECT COUNT(*) FROM segments WHERE media IN (1,2) AND source IS ? AND ts>=? AND ts<?",
                                             (src, s["t0"], s["t1"])).fetchone()[0]})
    for r in d.execute("SELECT * FROM media_plays WHERE started<? AND COALESCE(ended,last_seen)>=?", (b, a)):
        items.append({"kind": "play", "src": f"{r['source']}:{r['player']}", "t0": r["started"], "t1": r["ended"] or r["last_seen"],
                      "title": r["title"], "artist": r["artist"], "album": r["album"], "live": r["ended"] is None})
    for r in d.execute("SELECT * FROM media_tracks WHERE ts0<? AND ts1>=?", (b, a)):
        items.append({"kind": "track", "src": "fingerprint", "t0": r["ts0"], "t1": r["ts1"], "title": r["title"] or "",
                      "artist": r["artist"] or "", "status": r["status"]})
    items.sort(key=lambda x: -x["t0"])
    for x in items:
        x["day"] = datetime.fromtimestamp(x["t0"]).strftime("%Y-%m-%d")
        x["t"] = f"{fmt12(x['t0'])} to {fmt12(x['t1'])}"
        x["mins"] = round((x["t1"] - x["t0"]) / 60, 1)
    return {"items": items, "music_id": musicid.status(), "sources": [s.describe() for s in players.SOURCES],
            "cast_devices": json.loads(dbm.get_meta(d, "cast_devices", "[]") or "[]")}


@app.get("/media")
def media_page():
    return FileResponse(HERE / "static" / "media.html", media_type="text/html")


@app.get("/api/presence")
def presence_day(day: str = ""):
    """Who was in the room when (inferred from voices; TV voices excluded) plus the day's quiet-hour spans."""
    day = day or datetime.now().strftime("%Y-%m-%d")
    a, b = day_bounds(day)
    d = db()
    return {"day": day, "start": a, "end": b, "people": presmod.intervals(d, a, b), "quiet": presmod.quiet_spans(day),
            "quiet_cfg": presmod.quiet_cfg(), "now": presmod.present_now(d), "pad": presmod.PAD, "merge": presmod.MERGE}


@app.get("/api/quiet")
def quiet_get():
    return presmod.quiet_cfg()


@app.post("/api/quiet")
async def quiet_set(request: Request):
    """Quiet hours are a flag only: recording is unchanged. {"enabled": true, "start": "23:00", "end": "07:00"}"""
    b = await request.json()
    try:
        q = presmod.set_quiet(b.get("enabled", True), str(b.get("start", "23:00")), str(b.get("end", "07:00")), "site:" + who_from(request))
    except (ValueError, AttributeError):
        raise HTTPException(400, "start and end must be HH:MM")
    hub.publish("reload", {})
    return {"ok": True, "quiet": q}


@app.get("/api/health")
def health_report():
    from . import health
    return health.report(db())


@app.get("/health")
def health_page():
    return FileResponse(HERE / "static" / "health.html", media_type="text/html")


@app.get("/api/digest")
def digest(end: str = "", days: int = 7):
    """Weekly digest (local page only, nothing is sent anywhere): top ideas, open action items, what played,
    who talked, and each day's recap headline."""
    days = max(1, min(days, 31))
    end_d = datetime.strptime(end, "%Y-%m-%d") if end else datetime.now()
    end_day = end_d.strftime("%Y-%m-%d")
    start_day = (end_d - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    a, _ = day_bounds(start_day)
    _, b = day_bounds(end_day)
    d = db()
    ideas_out = []
    for r in d.execute("SELECT * FROM ideas WHERE created>=? AND created<? AND status!='dismissed'", (a, b)).fetchall():
        sb = d.execute("SELECT body FROM storyboards WHERE idea_id=? AND status='ok' ORDER BY version DESC LIMIT 1", (r["id"],)).fetchone()
        body = json.loads(sb["body"]) if sb else {}
        sc = (body.get("score") or {})
        ideas_out.append({"id": r["id"], "title": body.get("title") or r["title"], "pitch": body.get("pitch", ""), "status": r["status"],
                          "score": sc.get("value"), "verdict": sc.get("verdict"), "created": r["created"],
                          "lines": d.execute("SELECT COUNT(*) FROM idea_lines WHERE idea_id=?", (r["id"],)).fetchone()[0]})
    ideas_out.sort(key=lambda x: (-(x["score"] or 0), -x["created"]))
    open_items = [ai_row(d, r) for r in d.execute(
        "SELECT * FROM action_items WHERE sent=0 AND (due IS NULL OR due>=?) ORDER BY COALESCE(due,'9999'), source_ts LIMIT 60",
        ((end_d - timedelta(days=30)).strftime("%Y-%m-%d"),))]
    overdue = [x for x in open_items if x["due"] and x["due"] < datetime.now().strftime("%Y-%m-%d")]
    sess = mediamod.sessions(d, a, b)
    plays = [dict(r) for r in d.execute("""SELECT title, artist, source, player, COUNT(*) n, SUM(COALESCE(ended,last_seen)-started) secs
                                           FROM media_plays WHERE started>=? AND started<? GROUP BY title, artist ORDER BY secs DESC LIMIT 12""", (a, b))]
    talk = [{"name": r["name"] or "Unknown", "color": spk.color_for(r["speaker_id"], r["named"] or 0, r["color"]), "secs": round(r["secs"] or 0), "lines": r["n"]}
            for r in d.execute("""SELECT s.speaker_id, sp.name, sp.named, sp.color, SUM(s.dur) secs, COUNT(*) n FROM segments s
                                  LEFT JOIN speakers sp ON sp.id=s.speaker_id WHERE s.day>=? AND s.day<=? AND s.noise=0
                                  AND COALESCE(s.media,0) NOT IN (1,2) GROUP BY s.speaker_id ORDER BY secs DESC LIMIT 8""", (start_day, end_day))]
    heads = []
    for r in d.execute("SELECT day, body FROM summaries WHERE day>=? AND day<=? ORDER BY day, version", (start_day, end_day)):
        heads = [h for h in heads if h["day"] != r["day"]] + [{"day": r["day"], "headline": json.loads(r["body"]).get("headline", "")}]
    return {"start": start_day, "end": end_day, "days": days, "ideas": ideas_out[:10], "action_items": open_items, "overdue": len(overdue),
            "media": {"tv_secs": round(sum(s["t1"] - s["t0"] for s in sess if s["kind"] == "tv")),
                      "music_secs": round(sum(s["t1"] - s["t0"] for s in sess if s["kind"] == "music")),
                      "sessions": len(sess), "plays": plays,
                      "tracks": [dict(r) for r in d.execute("SELECT title, artist, status FROM media_tracks WHERE ts0>=? AND ts0<? ORDER BY ts0 DESC LIMIT 12", (a, b))]},
            "talk": talk, "recaps": heads,
            "lines": d.execute("SELECT COUNT(*) FROM segments WHERE day>=? AND day<=? AND noise=0 AND COALESCE(media,0) NOT IN (1,2)", (start_day, end_day)).fetchone()[0]}


@app.get("/digest")
def digest_page():
    return FileResponse(HERE / "static" / "digest.html", media_type="text/html")


# ---- B3-0 iPhone ramble capture: tailnet only (tailscale serve), plus a key the Shortcut sends ----
def _upload_key_ok(request):
    from . import uploads
    key = uploads.ensure_key()
    origin, host = request.headers.get("origin"), request.headers.get("host", "")
    if origin and origin.split("://", 1)[-1] == host:
        return True     # the Ramblr site itself (same origin on the tailnet: no login, like every other page)
    got = request.headers.get("authorization", "")
    got = got[7:] if got.lower().startswith("bearer ") else request.headers.get("x-ramblr-key", "")
    import hmac
    return bool(key) and hmac.compare_digest(got.strip(), key)


# ---- B4-19 extra mics over the tailnet: authenticated PCM stream relayed to ramblr-mics (Whisper lives there) ----
def _mic_key_ok(request):
    import hmac
    from . import uploads
    if not config.MIC_KEY:
        config.MIC_KEY = uploads.ensure_secret("RAMBLR_MIC_KEY", "extra mic key (B4-19): clients send it as 'Authorization: Bearer <key>'")
    got = request.headers.get("authorization", "")
    got = got[7:].strip() if got.lower().startswith("bearer ") else ""
    return bool(config.MIC_KEY) and hmac.compare_digest(got, config.MIC_KEY)


@app.post("/api/mic/stream")
async def mic_stream(request: Request, device: str = ""):
    """Body: raw s16le 16 kHz mono, as long as the mic runs (chunked upload, e.g. `curl -T -`).
    'Authorization: Bearer <RAMBLR_MIC_KEY>'. Lines land on the same timeline tagged mic:<device>."""
    from . import remote
    if not _mic_key_ok(request):
        raise HTTPException(401, "mic key required")
    if not remote.DEVICE_RE.match(device):
        raise HTTPException(400, "device: a-z, 0-9 and dash, up to 32")
    try:
        reader, writer = await asyncio.open_unix_connection(str(config.MICS_SOCK))
    except OSError:
        raise HTTPException(503, "ramblr-mics is not running")
    try:
        writer.write(json.dumps({"device": device, "rate": remote.RATE}).encode() + b"\n")
        await writer.drain()
        ack = (await asyncio.wait_for(reader.readline(), 120)).decode().strip()   # first stream loads Whisper
        if ack != "ok":
            raise HTTPException(409 if "already" in ack else 400, ack.removeprefix("err ").strip() or "refused")
        try:
            async for chunk in request.stream():
                writer.write(chunk)
                await writer.drain()
            writer.write_eof()
            done = json.loads((await asyncio.wait_for(reader.readline(), 300)) or b"{}")   # after the last line is stored
        except (ConnectionResetError, BrokenPipeError):
            raise HTTPException(503, "ramblr-mics restarted; reconnect")   # the client's unit reconnects in ~10 s
        if done.get("seconds") is None:
            raise HTTPException(500, "stream failed in ramblr-mics (see its journal)")
        return {"ok": True, "device": device, **done}
    finally:
        writer.close()


@app.get("/api/mics/remote")
def mics_remote():
    from . import remote
    snap = remote.status()
    return {"service": "running" if snap is not None else "not running", "devices": snap or {},
            "stream_url": f"{config.TAILNET_URL}/api/mic/stream?device=<name>", "key_file": "~/.config/ramblr/secrets.env (RAMBLR_MIC_KEY)"}


@app.post("/api/upload")
async def upload(request: Request, name: str = "", recorded_at: str = "", source: str = "iPhone"):
    """Raw audio body (m4a / caf / wav), Content-Type audio/*; 'Authorization: Bearer <RAMBLR_UPLOAD_KEY>'.
    The file waits in the tmpfs spool, is transcribed on the NPU by ramblr-ideas, then removed."""
    from . import uploads
    if not _upload_key_ok(request):
        raise HTTPException(401, "upload key required")
    mime = request.headers.get("content-type", "").split(";")[0].strip().lower()
    ext = config.UPLOAD_TYPES.get(mime) or {"m4a": "m4a", "caf": "caf", "wav": "wav"}.get(name.rsplit(".", 1)[-1].lower() if "." in name else "")
    if not ext:
        raise HTTPException(415, "send m4a, caf or wav (Content-Type audio/mp4, audio/x-caf or audio/wav)")
    uploads.INBOX.mkdir(parents=True, exist_ok=True)
    os.chmod(uploads.INBOX, 0o700)
    fname = f"up-{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}.{ext}"
    path = uploads.INBOX / fname
    size = 0
    try:
        with open(path, "xb") as f:                 # exclusive: never another upload's file
            async for chunk in request.stream():
                size += len(chunk)
                if size > config.UPLOAD_MAX:
                    raise HTTPException(413, f"over {config.UPLOAD_MAX // 2**20} MB")
                f.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)                # a dropped connection never leaves a stray file in the tmpfs inbox
        raise
    if size < 1000:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "empty recording")
    rec = None
    if recorded_at:
        try:
            rec = datetime.fromisoformat(recorded_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            rec = None
    src = "iPhone" if source not in ("iPhone", "upload") else source
    uid = db().execute("INSERT INTO uploads(source,name,bytes,mime,recorded_at,who,created,file) VALUES(?,?,?,?,?,?,?,?)",
                       (src, name[:120], size, mime, rec, who_from(request), time.time(), fname)).lastrowid
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.sendto(json.dumps({"t": "upload", "id": uid}).encode(), str(config.IDEAS_SOCK))
        s.close()
    except OSError:
        pass
    hub.publish("upload", {"id": uid})
    return {"ok": True, "upload_id": uid, "bytes": size, "status": "queued", "page": f"{config.TAILNET_URL}/iphone"}


@app.get("/api/uploads")
def uploads_list(limit: int = 30):
    return [dict(r) | {"file": None} for r in db().execute("SELECT * FROM uploads ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),))]


@app.get("/iphone")
def iphone_page():
    return FileResponse(HERE / "static" / "iphone.html", media_type="text/html")


@app.get("/iphone/ramblr.shortcut")
def iphone_shortcut():
    """The signed Shortcut (it carries the upload key, so it is only ever served on the tailnet)."""
    p = config.DATA / "iphone" / "ramblr.shortcut"
    if not p.exists():
        raise HTTPException(404, "no signed Shortcut yet; follow the build notes on /iphone")
    return FileResponse(p, media_type="application/octet-stream", filename="Ramblr.shortcut")


@app.get("/api/iphone/status")
def iphone_status():
    from . import uploads
    uploads.ensure_key()
    return {"upload_url": f"{config.TAILNET_URL}/api/upload", "key_file": "~/.config/ramblr/secrets.env (RAMBLR_UPLOAD_KEY)",
            "shortcut": (config.DATA / "iphone" / "ramblr.shortcut").exists(), "max_mb": config.UPLOAD_MAX // 2**20}


@app.get("/api/iphone/qr.svg")
def iphone_qr(target: str = "page"):
    """QR for the install page (or the signed Shortcut) on the tailnet. Contains a URL, never the key."""
    import io
    import segno
    url = f"{config.TAILNET_URL}/iphone" if target == "page" else f"{config.TAILNET_URL}/iphone/ramblr.shortcut"
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=6, dark="#2de2e6", light="#05070d", border=2)
    return HTMLResponse(buf.getvalue().decode(), media_type="image/svg+xml")


@app.get("/api/segments/{seg_id}/audio")
def segment_audio(seg_id: int):
    """B3-15: the line's clip from the rolling 24 h store (tailnet only). Lines older than 24 h have none."""
    from fastapi import Response
    from . import audio24
    r = db().execute("SELECT ts, dur FROM segments WHERE id=?", (seg_id,)).fetchone()
    if not r:
        raise HTTPException(404, "no such line")
    if time.time() - r["ts"] >= 86400:
        raise HTTPException(410, "audio is only kept for 24 hours")
    data = audio24.clip(r["ts"], r["dur"])
    if not data:
        raise HTTPException(404, "no audio for this line (recorded before the 24 h store existed, while muted, or over the cap)")
    return Response(data, media_type="audio/ogg", headers={"Cache-Control": "private, max-age=300"})


@app.get("/api/retention")
def retention_status():
    from . import retention
    return retention.status(db())


@app.get("/api/live")
def live_summary():
    """The rolling live summary of the conversation happening now (B3-7) + the latest closed card."""
    d = db()
    st = json.loads(dbm.get_meta(d, "live_summary", "null") or "null")
    cur = d.execute("SELECT * FROM conversations WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
    last = d.execute("SELECT * FROM conversations WHERE status='closed' AND card IS NOT NULL AND card!='{}' ORDER BY id DESC LIMIT 1").fetchone()
    return {"open": dict(cur) | {"people": None} if cur else None,
            "live": st if (st and cur and st.get("conv_id") == cur["id"]) else None,
            "last_card": {"id": last["id"], "ts0": last["ts0"], "ts1": last["ts1"], "card": json.loads(last["card"]),
                          "people": json.loads(last["people"] or "[]"), "first_seg": last["first_seg"]} if last else None,
            "now": time.time()}


@app.get("/api/conversations")
def conversations_day(day: str = ""):
    from . import conversations as convmod
    return convmod.cards_for_day(db(), day or datetime.now().strftime("%Y-%m-%d"))


@app.post("/api/segments/{seg_id}/text")
async def fix_text(seg_id: int, request: Request):
    """Fred's fix for a line's words. The original stays in text_orig; the change teaches the vocabulary."""
    from . import vocab
    body = await request.json()
    out = run_op(lambda: vocab.correct_line(db(), seg_id, str(body.get("text", "")), who_from(request)))
    r = db().execute(SEG_SQL + " WHERE s.id=?", (seg_id,)).fetchone()
    if r:
        hub.publish("upd", row(r))
    return out


@app.get("/api/vocab")
def vocab_list():
    d = db()
    return {"entries": [dict(r) for r in d.execute("SELECT * FROM vocab ORDER BY active DESC, hits DESC, id DESC")],
            "edits": d.execute("SELECT COUNT(*) FROM text_edits").fetchone()[0],
            "prompt_words": __import__("ramblr.vocab", fromlist=["x"]).prompt_words(d)}


@app.post("/api/vocab")
async def vocab_add(request: Request):
    from . import vocab
    b = await request.json()
    if b.get("id") is not None:
        return run_op(lambda: (vocab.set_active(db(), int(b["id"]), bool(b.get("active"))), {})[1])
    return run_op(lambda: (vocab.add_word(db(), str(b.get("word", "")), b.get("wrong") or None), {})[1])


# ---- B3-8/9/10 insights: facts, goals, weekly patterns, topic trackers, talk stats ----
@app.get("/api/insights")
def insights_all(days: int = 30):
    from . import insights as ins
    d = db()
    days = max(7, min(days, 180))
    facts = [dict(r) | {"vec": None, "line_ids": json.loads(r["line_ids"])} for r in d.execute(
        "SELECT * FROM facts ORDER BY status='active' DESC, mentions DESC, last_seen DESC LIMIT 400")]
    goals = [dict(r) | {"line_ids": json.loads(r["line_ids"])} for r in d.execute(
        "SELECT * FROM goals ORDER BY status='open' DESC, last_seen DESC LIMIT 200")]
    pat = d.execute("SELECT week, body, model, created FROM insights ORDER BY id DESC LIMIT 6").fetchall()
    return {"facts": facts, "goals": goals, "patterns": [{"week": r["week"], "insights": json.loads(r["body"]), "model": r["model"]} for r in pat],
            "trackers": ins.trackers(d, days), "talk": ins.talk_stats(d, days), "energy": ins.energy_map(d, days)}


@app.post("/api/facts/{fid}/status")
async def fact_status(fid: int, request: Request):
    st = (await request.json()).get("status")
    if st not in ("active", "rejected"):
        raise HTTPException(400, "status must be active or rejected")
    if db().execute("UPDATE facts SET status=? WHERE id=?", (st, fid)).rowcount == 0:
        raise HTTPException(404, "no such fact")
    return {"ok": True}


@app.post("/api/goals/{gid}/status")
async def goal_status(gid: int, request: Request):
    st = (await request.json()).get("status")
    if st not in ("open", "done", "dropped"):
        raise HTTPException(400, "status must be open, done or dropped")
    if db().execute("UPDATE goals SET status=? WHERE id=?", (st, gid)).rowcount == 0:
        raise HTTPException(404, "no such goal")
    return {"ok": True}


@app.post("/api/trackers")
async def tracker_add(request: Request):
    from . import insights as ins
    b = await request.json()
    if b.get("id") is not None:
        if db().execute("UPDATE topic_trackers SET active=? WHERE id=?", (1 if b.get("active") else 0, int(b["id"]))).rowcount == 0:
            raise HTTPException(404, "no such tracker")
        return {"ok": True}
    return run_op(lambda: (ins.add_tracker(db(), str(b.get("name", "")), str(b.get("query", "")), bool(b.get("phrase"))), {})[1])


@app.get("/api/folders")
def folders_list():
    from . import folders
    return {"folders": folders.listing(db()), "names": folders.names(db())}


@app.post("/api/folders/move")
async def folders_move(request: Request):
    """Move a conversation card or an idea to a folder (made if new). Manual placements are never re-sorted."""
    from . import folders
    b = await request.json()
    kind, iid, name = b.get("item_type"), b.get("item_id"), str(b.get("folder", ""))
    if kind not in ("conversation", "idea") or not isinstance(iid, int) or not name.strip():
        raise HTTPException(400, "item_type conversation|idea, integer item_id and a folder name")
    folders.place(db(), kind, iid, name, source="manual")
    return {"ok": True}


@app.get("/api/plugins")
def plugins_status():
    from . import events, plugins
    d = db()
    ps, errors = plugins.load_all()
    runs = [dict(r) for r in d.execute("SELECT * FROM plugin_runs ORDER BY id DESC LIMIT 40")]
    hooks = events.hooks()
    return {"dir": "~/.config/ramblr/plugins", "plugins": [{k: p[k] for k in ("id", "name", "schedule", "input", "file")} for p in ps],
            "errors": errors, "runs": runs, "webhooks": [{"events": h.get("events") or "all", "signed": bool(h.get("secret_env"))} for h in hooks],
            "events": [dict(r) | {"payload": None} for r in d.execute("SELECT id, kind, created FROM events ORDER BY id DESC LIMIT 20")],
            "deliveries": [dict(r) for r in d.execute("SELECT event_id, hook, status, tries, error, sent_at FROM deliveries ORDER BY event_id DESC LIMIT 20")]}


@app.get("/plugins")
def plugins_page():
    return FileResponse(HERE / "static" / "plugins.html", media_type="text/html")


# ---- B3-13 follow-up drafts: kept in Ramblr, never sent by it ----
@app.get("/api/drafts")
def drafts_list(status: str = ""):
    d = db()
    where = "WHERE status=?" if status else ""
    rows = d.execute(f"SELECT * FROM drafts {where} ORDER BY id DESC LIMIT 200", (status,) if status else ()).fetchall()
    out = []
    for r in rows:
        c = d.execute("SELECT ts0, first_seg, card FROM conversations WHERE id=?", (r["conversation_id"],)).fetchone() if r["conversation_id"] else None
        out.append(dict(r) | {"conversation_title": json.loads(c["card"]).get("title") if c and c["card"] and c["card"] != "{}" else None,
                              "conversation_ts": c["ts0"] if c else None, "first_seg": c["first_seg"] if c else None})
    return out


@app.post("/api/drafts/{did}")
async def draft_update(did: int, request: Request):
    from . import drafts
    b = await request.json()
    return run_op(lambda: drafts.update(db(), did, b.get("body"), b.get("status"), who_from(request)))


@app.post("/api/conversations/{cid}/drafts")
async def draft_make(cid: int):
    from . import drafts
    return await asyncio.to_thread(lambda: run_op(lambda: drafts.make_for_conversation(db(), cid, force=True)))


@app.get("/drafts")
def drafts_page():
    return FileResponse(HERE / "static" / "drafts.html", media_type="text/html")


@app.get("/api/commands")
def commands_list(limit: int = 200):
    from . import commands
    url, key = commands.router()
    rows = db().execute("""SELECT id, command_id, segment_id, ts, target, task, speaker, confidence, status, result, tries, sent_at, phrase
                           FROM command_intents WHERE command_id IS NOT NULL ORDER BY id DESC LIMIT ?""", (max(1, min(limit, 1000)),)).fetchall()
    return {"router": "gus voice router (tailnet only; actions wait for your yes)" if url and key else "stub (no RAMBLR_VOICE_ROUTER_URL/KEY in secrets.env)",
            "commands": [dict(r) for r in rows]}


@app.post("/api/commands/{command_id}/approve")
def commands_approve(command_id: str):
    """Fred's yes for a held voice command. The router decides what happens next; nothing else can release it."""
    from . import commands
    try:
        st = commands.approve(db(), command_id)
    except LookupError:
        raise HTTPException(404, "unknown command")
    except Exception as e:
        raise HTTPException(502, f"router: {type(e).__name__}")
    hub.publish("command", {"id": command_id})
    return {"ok": True, "status": st}


@app.get("/commands")
def commands_page():
    return FileResponse(HERE / "static" / "commands.html", media_type="text/html")


# ---- B4-1 imports: Zoom transcripts and Wispr dictations onto the same timeline ----
@app.post("/api/imports")
async def import_upload(request: Request, kind: str, name: str = "", start: str = ""):
    from . import imports
    if kind not in ("zoom", "wispr"):
        raise HTTPException(400, "kind must be zoom or wispr")
    raw = await request.body()
    if len(raw) > 20 * 2**20:
        raise HTTPException(413, "over 20 MB")
    st = None
    if start:
        try:
            st = datetime.fromisoformat(start).timestamp()
        except ValueError:
            raise HTTPException(400, "start must be ISO, e.g. 2026-10-24T09:30")
    out = run_op(lambda: imports.import_file(db(), kind, name or f"{kind}-upload", raw.decode("utf-8", errors="replace"), st))
    return out


@app.get("/api/imports")
def imports_list():
    from . import imports
    d = db()
    return {"imports": [dict(r) | {"result": json.loads(r["result"])} for r in d.execute("SELECT * FROM imports ORDER BY created DESC LIMIT 100")],
            "inbox": str(imports.INBOX).replace(str(config.HOME), "~"), "zoom_api": imports.zoom_api_status(),
            "wispr_api": "no public Wispr API: export the history and import the file",
            "lines": {r[0]: r[1] for r in d.execute("SELECT source, COUNT(*) FROM segments WHERE source IN ('Zoom','Wispr') GROUP BY source")}}


@app.get("/imports")
def imports_page():
    return FileResponse(HERE / "static" / "imports.html", media_type="text/html")


# ---- B4-3 promises, B4-4 morning brief, B4-6 calendar, B4-13 ambient context ----
@app.get("/api/promises")
def promises(include_done: int = 0):
    """'What did I promise': action items Fred owns (his confirmed voice, else 'Fred...'), soonest due first."""
    from . import me
    d = db()
    names = sorted(me.names(d))
    ph = ",".join("?" * len(names))
    where = f"lower(owner) IN ({ph}) OR lower(owner) LIKE 'fred%' OR lower(owner) IN ('me','i')"
    rows = d.execute(f"SELECT * FROM action_items WHERE ({where})" + ("" if include_done else " AND done_at IS NULL") +
                     " ORDER BY done_at IS NOT NULL, COALESCE(due,'9999'), source_ts DESC LIMIT 300", names).fetchall()
    today = datetime.now().strftime("%Y-%m-%d")
    out = [ai_row(d, r) | {"done_at": iso(r["done_at"]) if r["done_at"] else None,
                           "overdue": bool(r["due"] and r["due"] < today and not r["done_at"])} for r in rows]
    return {"me": names, "items": out, "overdue": sum(x["overdue"] for x in out),
            "due_today": sum(1 for x in out if x["due"] == today and not x["done_at"])}


@app.post("/api/action-items/{aid}/done")
async def action_item_done(aid: int, request: Request):
    done = bool((await request.json()).get("done", True))
    d = db()
    if d.execute("UPDATE action_items SET done_at=? WHERE id=?", (time.time() if done else None, aid)).rowcount == 0:
        raise HTTPException(404, "no such action item")
    return {"ok": True, "done": done}


@app.get("/api/calendar/events")
def calendar_events(day: str = ""):
    from . import calendar_feed
    day = day or datetime.now().strftime("%Y-%m-%d")
    a, b = day_bounds(day)
    return {"status": calendar_feed.status(), "events": calendar_feed.between(db(), a, b)}


@app.get("/api/ambient")
def ambient_day(day: str = ""):
    from . import ambient
    day = day or datetime.now().strftime("%Y-%m-%d")
    a, b = day_bounds(day)
    return {"snapshots": ambient.for_day(db(), a, b), "weather": "on" if os.environ.get("RAMBLR_WEATHER_LATLON") else "off"}


@app.get("/api/brief")
def morning_brief():
    """B4-4: one page to start the day, built from data Ramblr already has (no model call)."""
    from . import calendar_feed, ideatime
    d = db()
    now = datetime.now()
    today, yday = now.strftime("%Y-%m-%d"), (now - timedelta(days=1)).strftime("%Y-%m-%d")
    a, b = day_bounds(today)
    pr = promises()
    sm = d.execute("SELECT body FROM summaries WHERE day=? ORDER BY version DESC LIMIT 1", (yday,)).fetchone()
    hot = [dict(r) for r in d.execute("SELECT id, title, heat, mentions, status FROM ideas WHERE status IN ('new','later') ORDER BY COALESCE(heat,0) DESC LIMIT 3")]
    cards = [json.loads(r["card"]) | {"ts0": r["ts0"]} for r in d.execute(
        "SELECT card, ts0 FROM conversations WHERE ts0>=? AND card IS NOT NULL AND card!='{}' ORDER BY ts0 DESC LIMIT 3", (a - 86400,))]
    return {"date": now.strftime("%A, %B %-d"), "weather": dbm.get_meta(d, "weather_now"),
            "calendar": {"status": calendar_feed.status(), "events": calendar_feed.between(d, a, b)},
            "promises": [x for x in pr["items"] if x["overdue"] or x["due"] == today][:10], "overdue": pr["overdue"], "due_today": pr["due_today"],
            "open_promises": len(pr["items"]),
            "yesterday": json.loads(sm["body"]) if sm else None, "hot_ideas": hot, "recent_cards": cards,
            "drafts_waiting": d.execute("SELECT COUNT(*) FROM drafts WHERE status='draft'").fetchone()[0],
            "goals_open": d.execute("SELECT COUNT(*) FROM goals WHERE status='open'").fetchone()[0],
            "review": len(ideatime.review(d, 1))}


@app.get("/brief")
def brief_page():
    return FileResponse(HERE / "static" / "brief.html", media_type="text/html")


@app.get("/promises")
def promises_page():
    return FileResponse(HERE / "static" / "promises.html", media_type="text/html")


# ---- B4-11 podcast quote pull ----
@app.post("/api/quotes")
async def quotes_pull(request: Request):
    from . import quotes
    b = await request.json()
    a = parse_local(b["start"]) if b.get("start") else None
    z = parse_local(b["end"]) if b.get("end") else None
    if b.get("segment_ids"):
        ids = [int(x) for x in b["segment_ids"]]
        ph = ",".join("?" * len(ids))
        r = db().execute(f"SELECT MIN(ts), MAX(ts+dur) FROM segments WHERE id IN ({ph})", ids).fetchone()
        a, z = r[0], r[1]
    return await asyncio.to_thread(lambda: run_op(lambda: quotes.pull(db(), a, z, str(b.get("topic", ""))[:200])))


@app.get("/api/quotes")
def quotes_list():
    return [dict(r) | {"quotes": json.loads(r["quotes"])} for r in db().execute("SELECT * FROM quote_pulls ORDER BY id DESC LIMIT 50")]


@app.get("/api/quotes/{pid}.md", response_class=PlainTextResponse)
def quotes_md(pid: int):
    from . import quotes
    try:
        return PlainTextResponse(quotes.markdown(db(), pid), media_type="text/markdown; charset=utf-8",
                                 headers={"Content-Disposition": f'inline; filename="ramblr-quotes-{pid}.md"'})
    except LookupError:
        raise HTTPException(404, "no such pull")


@app.get("/quotes")
def quotes_page():
    return FileResponse(HERE / "static" / "quotes.html", media_type="text/html")


# ---- B4-14 family archive: its own DB file, its own page, no AI ----
def fdb():
    if not hasattr(_local, "fdb"):
        _local.fdb = dbm.connect(config.FAMILY_DB)
        os.chmod(config.FAMILY_DB, 0o600)
    return _local.fdb


@app.post("/api/family")
async def family_toggle(request: Request):
    on = bool((await request.json()).get("on"))
    state.update(by="site:" + who_from(request), family=on)
    hub.publish("status", status())
    return {"ok": True, "family": on}


@app.get("/api/family/lines")
def family_lines(day: str = "", q: str = ""):
    d = fdb()
    if q.strip():
        like = "%" + q.strip().replace("%", "") + "%"
        rows = d.execute("SELECT id, ts, day, text FROM segments WHERE text LIKE ? ORDER BY ts DESC LIMIT 500", (like,)).fetchall()
    else:
        day = day or (d.execute("SELECT MAX(day) FROM segments").fetchone()[0] or datetime.now().strftime("%Y-%m-%d"))
        rows = d.execute("SELECT id, ts, day, text FROM segments WHERE day=? ORDER BY ts", (day,)).fetchall()
    days = [r[0] for r in d.execute("SELECT DISTINCT day FROM segments ORDER BY day DESC")]
    return {"day": day, "days": days, "lines": [{"id": r["id"], "t": fmt(r["ts"]), "day": r["day"], "text": r["text"]} for r in rows]}


@app.get("/family")
def family_page():
    return FileResponse(HERE / "static" / "family.html", media_type="text/html")


@app.get("/setup")
def setup_page():
    return FileResponse(HERE / "static" / "setup.html", media_type="text/html")


@app.post("/api/setup/done")
def setup_done():
    dbm.set_meta(db(), "setup_done", time.time())
    return {"ok": True}


@app.get("/folders")
def folders_page():
    return FileResponse(HERE / "static" / "folders.html", media_type="text/html")


@app.get("/insights")
def insights_page():
    return FileResponse(HERE / "static" / "insights.html", media_type="text/html")


@app.get("/api/media/status")
def media_status():
    d = db()
    last = d.execute("SELECT ts1, music, tv, top FROM media_windows ORDER BY id DESC LIMIT 1").fetchone()
    return {"device": dbm.get_meta(d, "media_device"), "model": mediamod.load_model(d),
            "windows": d.execute("SELECT COUNT(*) FROM media_windows").fetchone()[0],
            "last_window": dict(last) if last else None,
            "lines": {k: v for k, v in d.execute("SELECT COALESCE(media,'pending'), COUNT(*) FROM segments GROUP BY 1").fetchall()}}


@app.post("/api/suggestions/{gid}/{decision}")
async def suggestion(gid: int, decision: str, request: Request):
    if decision not in ("accept", "reject"):
        raise HTTPException(404)
    return run_op(lambda: spk.decide_suggestion(db(), gid, decision == "accept", who_from(request)))


@app.post("/api/audit/{batch_id}/undo")
async def undo(batch_id: str, request: Request):
    return run_op(lambda: spk.undo(db(), batch_id, who_from(request)))


@app.get("/api/palette")
def palette():
    return {"palette": spk.PALETTE, "unnamed": spk.GRAY}


@app.get("/api/audit")
def audit(limit: int = 40):
    return spk.history(db(), max(1, min(limit, 200)))



# ---- phase 2: mute, action items, summaries, ask, markdown export ----
@app.get("/api/call")
def call_get():
    from . import calls
    return calls.status(db())


@app.post("/api/call")
async def call_set(request: Request):
    """The site's Call button: {"on": true|false, "hours": optional}. On holds until switched off, with
    CALL_SWITCH_HOURS (4 h) as the safety net. Call mode is also on while a heartbeat or gus itself sees a call."""
    from . import calls
    body = await request.json()
    on = bool(body.get("on"))
    hours = min(max(float(body.get("hours") or config.CALL_SWITCH_HOURS), 0.1), 12.0)
    state.update(by="site:" + who_from(request), call_until=(time.time() + hours * 3600) if on else 0)
    hub.publish("status", status())
    return {"ok": True, **calls.status(db())}


@app.get("/api/speakers/suggestions")
def speaker_suggestions():
    return spk.suggestions(db())


@app.post("/api/speakers/suggestions/dismiss")
async def speaker_suggestion_dismiss(request: Request):
    b = await request.json()
    return run_op(lambda: spk.dismiss_suggestion(db(), int(b["cluster_id"]), int(b["person_id"])))


@app.post("/api/mute")
async def mute(request: Request):
    """{"muted": true|false} or {"toggle": true}. Muted stops the recorder itself (capture follows within ~1 s)."""
    body = await request.json()
    cur = state.load().get("muted")
    new = (not cur) if body.get("toggle") else bool(body.get("muted"))
    state.update(by="site:" + who_from(request), muted=new)
    hub.publish("status", status())
    return {"ok": True, "muted": new}


def iso(ts):
    return None if ts is None else datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def ai_row(d, r):
    ids = json.loads(r["line_ids"])
    ph = ",".join("?" * len(ids))
    lines = d.execute(f"""SELECT s.id, s.ts, s.text, COALESCE(sp.name,'Unknown') AS speaker FROM segments s
                          LEFT JOIN speakers sp ON sp.id=s.speaker_id WHERE s.id IN ({ph}) ORDER BY s.ts""", ids).fetchall()
    return {"id": r["id"], "key": r["key"], "day": r["day"], "owner": r["owner"], "task": r["task"], "specifics": r["specifics"],
            "due": r["due"], "due_phrase": r["due_phrase"], "said_at": iso(r["source_ts"]), "created": iso(r["created"]),
            "sent": bool(r["sent"]), "sent_at": iso(r["sent_at"]), "model": r["model"],
            "source_line_ids": ids, "link": f"/?day={r['day']}#seg-{ids[0]}",
            "source_lines": [{"id": x["id"], "t": fmt(x["ts"]), "speaker": x["speaker"], "text": x["text"]} for x in lines]}


def parse_iso(s):
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "since must be ISO 8601, e.g. 2026-10-24T00:00:00-04:00")
    return dt.timestamp()


@app.get("/api/action-items")
def action_items(since: str = "", day: str = "", unsent: int = 0, limit: int = 1000):
    """Tailnet-only JSON for the iPhone side: stable ids, a 'sent' flag, links back to the source lines.
    since = items CREATED at or after this ISO time (the nightly pass creates them)."""
    where, args = [], []
    if since:
        where.append("created >= ?"); args.append(parse_iso(since))
    if day:
        where.append("day = ?"); args.append(day)
    if unsent:
        where.append("sent = 0")
    d = db()
    rs = d.execute("SELECT * FROM action_items" + (" WHERE " + " AND ".join(where) if where else "") +
                   " ORDER BY source_ts LIMIT ?", args + [max(1, min(limit, 10000))]).fetchall()
    return {"generated": iso(time.time()), "count": len(rs), "items": [ai_row(d, r) for r in rs]}


@app.post("/api/action-items/{aid}/sent")
def action_item_sent(aid: int):
    """Idempotent: the first call stamps sent_at, later calls return the same stamp."""
    d = db()
    d.execute("UPDATE action_items SET sent=1, sent_at=COALESCE(sent_at, ?) WHERE id=?", (time.time(), aid))
    r = d.execute("SELECT * FROM action_items WHERE id=?", (aid,)).fetchone()
    if not r:
        raise HTTPException(404, "no such action item")
    return {"ok": True, "item": ai_row(d, r)}


@app.get("/api/summaries/{day}")
def summary(day: str, v: int = 0):
    d = db()
    q = "SELECT * FROM summaries WHERE day=?" + (" AND version=?" if v else "") + " ORDER BY version DESC LIMIT 1"
    r = d.execute(q, (day, v) if v else (day,)).fetchone()
    versions = [x[0] for x in d.execute("SELECT version FROM summaries WHERE day=? ORDER BY version", (day,))]
    if not r:
        return {"day": day, "summary": None, "versions": versions}
    body = json.loads(r["body"])
    items = [ai_row(d, x) for x in d.execute("SELECT * FROM action_items WHERE day=? ORDER BY source_ts", (day,))]
    return {"day": day, "version": r["version"], "versions": versions, "created": iso(r["created"]), "model": r["model"],
            "lines": r["lines"], "summary": body, "action_items": items}


@app.get("/api/nightly")
def nightly_last():
    d = db()
    runs = [dict(r) for r in d.execute("SELECT id, day, started, finished, status FROM nightly_runs ORDER BY id DESC LIMIT 20")]
    return {"last": json.loads(dbm.get_meta(d, "nightly_last", "null") or "null"),
            "backup": json.loads(dbm.get_meta(d, "backup_last", "null") or "null"), "runs": runs}


def _ts_range(start, end):
    return (parse_local(start) if start else None), (parse_local(end) if end else None)


@app.get("/api/search/semantic")
def semantic_search(q: str, start: str = "", end: str = "", speaker_id: int = 0, k: int = 20):
    from . import search
    if not q.strip():
        raise HTTPException(400, "q required")
    d = db()
    a, b = _ts_range(start, end)
    hits = search.semantic(d, q.strip(), k=max(1, min(k, 50)), start=a, end=b, speaker_id=speaker_id or None)
    out = []
    for h in hits:
        ph = ",".join("?" * len(h["seg_ids"]))
        out.append({**h, "lines": [row(r) for r in d.execute(SEG_SQL + f" WHERE s.id IN ({ph}) ORDER BY s.ts", h["seg_ids"])]})
    return {"q": q, "hits": out}


@app.post("/api/ask")
async def ask(request: Request):
    from . import search
    q = " ".join(str((await request.json()).get("q", "")).split())[:500]
    if not q:
        raise HTTPException(400, "q required")
    return await asyncio.to_thread(lambda: search.ask(db(), q))   # db() inside the worker thread: one connection per thread


def md_escape(t):
    return t.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_").replace("`", "\\`").replace("<", "&lt;")


@app.get("/api/export.md", response_class=PlainTextResponse)
def export_md(day: str = "", start: str = "", end: str = "", noise: int = 0, media: int = 0):
    """Markdown of a day or a time range (local times), with that day's recap and action items. TV / music lines are
    left out unless media=1."""
    if day:
        rs = segments(day=day, noise=noise, media=media)
        title, fname = f"Ramblr: {day}", f"ramblr-{day}.md"
    elif start and end:
        rs = segments(start=start, end=end, noise=noise, media=media)
        title, fname = f"Ramblr: {start} to {end}", f"ramblr-{start.replace(':', '')}-{end.replace(':', '')}.md".replace(" ", "_")
    else:
        raise HTTPException(400, "give day=YYYY-MM-DD or start= and end=")
    d = db()
    out = [f"# {title}", "", f"_{len(rs)} lines, exported {datetime.now().strftime('%Y-%m-%d %H:%M')}_", ""]
    for dd in sorted({r["day"] for r in rs}):
        out += [f"## {dd}", ""]
        sm = d.execute("SELECT body FROM summaries WHERE day=? ORDER BY version DESC LIMIT 1", (dd,)).fetchone()
        if sm and day:
            b = json.loads(sm["body"])
            out += ["### Recap", "", b.get("headline", ""), ""]
            for k, label in (("decisions", "Decisions"), ("todos", "To-dos")):
                if b.get(k):
                    out += [f"**{label}**", ""] + [f"- {md_escape(str(x))}" for x in b[k]] + [""]
        ais = d.execute("SELECT owner, task, due FROM action_items WHERE day=? ORDER BY source_ts", (dd,)).fetchall() if day else []
        if ais:
            out += ["### Action items", ""] + [f"- [ ] {md_escape(a['task'])}" + (f" ({md_escape(a['owner'])})" if a["owner"] else "")
                                                + (f", due {a['due']}" if a["due"] else "") for a in ais] + [""]
        out += ["### Transcript", ""]
        for r in (x for x in rs if x["day"] == dd):
            out.append(f"**{r['t']} {md_escape(r['speaker'])}:** {md_escape(r['text'])}  ")
        out.append("")
    return PlainTextResponse("\n".join(out), media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": f'inline; filename="{fname}"'})


# ---- phase 2b: mic picker. Ramblr records the chosen source with its own parec stream; the system default
# source (Voxtype depends on it) is never changed. ----
def mics_payload():
    d = db()
    listing = micmod.list_sources(max_age=1.0)
    st = state.load()
    rec = dbm.get_meta(d, "capture_source", "")
    home = micmod.home_source(listing)
    return {"chosen": st.get("source") or None, "recording": rec or None, "home": home, "muted": bool(st.get("muted")),
            "warning": dbm.get_meta(d, "capture_warning", ""),
            "mics": [{**m, "recording": m["name"] == rec, "chosen": m["name"] == (st.get("source") or home),
                      "home": m["name"] == home} for m in listing]}


@app.get("/api/mics")
async def get_mics():
    return await asyncio.to_thread(mics_payload)


@app.post("/api/mics")
async def set_mic(request: Request):
    """{"source": "<node name>"} records that mic; {"source": null} goes back to the home mic (the BRIO)."""
    src = (await request.json()).get("source")
    if src:
        names = {m["name"] for m in await asyncio.to_thread(micmod.list_sources)}
        if src not in names:
            raise HTTPException(400, "unknown input source")
    state.update(by="site:" + who_from(request), source=src or None)
    for _ in range(30):                      # wait (max ~3 s) for capture to switch, so the reply is the truth
        await asyncio.sleep(0.1)
        if dbm.get_meta(db(), "capture_source", "") == (src or micmod.home_source(micmod.list_sources(max_age=5))):
            break
    hub.publish("status", status())
    return {"ok": True, **(await asyncio.to_thread(mics_payload))}


# ---- phase 3: data for the UI layer (timeline heatmap, calendar, people, search with filters, on this day) ----
def day_bounds(day):
    a = datetime.strptime(day, "%Y-%m-%d").timestamp()
    b = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).timestamp()   # DST-safe
    return a, b


@app.get("/api/timeline")
def timeline(day: str, bin: int = 300):
    """Talk seconds per time bin and per speaker for one day, for the glowing heatmap bar."""
    bin = max(60, min(int(bin), 3600))
    a, b = day_bounds(day)
    n = int((b - a) // bin)
    bins = [{"i": i, "t0": a + i * bin, "secs": 0.0, "by": {}, "first_id": None} for i in range(n)]
    for r in db().execute("SELECT id, ts, dur, speaker_id FROM segments WHERE day=? AND noise=0 AND COALESCE(media,0) NOT IN (1,2) ORDER BY ts", (day,)):
        i = int((r["ts"] - a) // bin)
        if 0 <= i < n:
            x = bins[i]
            x["secs"] += r["dur"]
            k = str(r["speaker_id"] or 0)
            x["by"][k] = round(x["by"].get(k, 0) + r["dur"], 1)
            if x["first_id"] is None:
                x["first_id"] = r["id"]
    for x in bins:
        x["secs"] = round(x["secs"], 1)
    marks = [{"ts": r["ts"], "kind": "idea"} for r in db().execute(
        "SELECT MIN(s.ts) ts FROM idea_lines il JOIN segments s ON s.id=il.segment_id WHERE s.day=? GROUP BY il.idea_id", (day,))]
    marks += [{"ts": r["source_ts"], "kind": "action"} for r in db().execute("SELECT source_ts FROM action_items WHERE day=?", (day,))]
    marks += [{"ts": r["ts1"], "kind": "conv"} for r in db().execute(
        "SELECT ts1 FROM conversations WHERE ts0>=? AND ts0<? AND card IS NOT NULL AND card!='{}'", (a, b))]
    spans = []      # media periods from the classifier, drawn as gray bands behind the bars
    for r in db().execute("SELECT ts0, ts1, music, tv FROM media_windows WHERE ts1>=? AND ts0<? AND (music>=0.5 OR tv>=0.4) ORDER BY ts0", (a, b)):
        kind = "music" if r["music"] >= r["tv"] else "tv"
        if spans and r["ts0"] - spans[-1]["t1"] < 30 and spans[-1]["kind"] == kind:
            spans[-1]["t1"] = r["ts1"]
        else:
            spans.append({"t0": r["ts0"], "t1": r["ts1"], "kind": kind})
    for x in bins:
        x["media_secs"] = 0.0
    for r in db().execute("SELECT ts, dur FROM segments WHERE day=? AND media IN (1,2)", (day,)):
        i = int((r["ts"] - a) // bin)
        if 0 <= i < n:
            bins[i]["media_secs"] = round(bins[i]["media_secs"] + r["dur"], 1)
    from . import calendar_feed
    return {"day": day, "bin": bin, "start": a, "max": max([x["secs"] for x in bins] + [0]), "bins": bins, "marks": marks,
            "media": spans, "calendar": [e for e in calendar_feed.between(db(), a, b) if not e["all_day"]]}


@app.get("/api/calendar")
def calendar(month: str = ""):
    """Per-day heat (talk seconds) and counts of ideas and action items for one month."""
    month = month or datetime.now().strftime("%Y-%m")
    d = db()
    out = {}
    for r in d.execute("SELECT day, SUM(dur) secs, COUNT(*) n FROM segments WHERE noise=0 AND day LIKE ? GROUP BY day", (month + "-%",)):
        out[r["day"]] = {"day": r["day"], "secs": round(r["secs"] or 0), "lines": r["n"], "ideas": 0, "actions": 0, "summary": False}
    def bump(day, key):
        out.setdefault(day, {"day": day, "secs": 0, "lines": 0, "ideas": 0, "actions": 0, "summary": False})[key] += 1
    for r in d.execute("""SELECT s.day FROM ideas i JOIN idea_lines il ON il.idea_id=i.id JOIN segments s ON s.id=il.segment_id
                          WHERE s.day LIKE ? GROUP BY i.id""", (month + "-%",)):
        bump(r["day"], "ideas")
    for r in d.execute("SELECT day FROM action_items WHERE day LIKE ?", (month + "-%",)):
        bump(r["day"], "actions")
    for r in d.execute("SELECT DISTINCT day FROM summaries WHERE day LIKE ?", (month + "-%",)):
        out.setdefault(r["day"], {"day": r["day"], "secs": 0, "lines": 0, "ideas": 0, "actions": 0})["summary"] = True
    days_with = sorted(r[0] for r in d.execute("SELECT DISTINCT substr(day,1,7) FROM segments WHERE noise=0"))
    return {"month": month, "days": sorted(out.values(), key=lambda x: x["day"]),
            "max_secs": max([x["secs"] for x in out.values()] + [0]), "months": days_with}


def person_core(d, r):
    tot = d.execute("SELECT COUNT(*) n, COALESCE(SUM(dur),0) secs, MIN(ts) a, MAX(ts) b FROM segments WHERE speaker_id=? AND noise=0",
                    (r["id"],)).fetchone()
    return {"id": r["id"], "name": r["name"], "named": bool(r["named"]), "confirmed": bool(r["confirmed"]),
            "color": spk.color_for(r["id"], r["named"], r["color"]), "custom_color": r["color"],
            "lines": tot["n"], "talk_secs": round(tot["secs"]), "first_seen": tot["a"], "last_seen": tot["b"],
            "voiceprint_samples": (d.execute("SELECT n FROM voiceprints WHERE speaker_id=?", (r["id"],)).fetchone() or [0])[0],
            "ambiguous_samples": [{"segment_id": x["segment_id"], "t": fmt(x["ts"]), "day": x["day"], "text": x["text"][:80]} for x in d.execute(
                """SELECT e.segment_id, s.ts, s.day, s.text FROM embeddings e JOIN segments s ON s.id=e.segment_id
                   WHERE e.counted_for=? AND e.ambiguous=1 ORDER BY s.ts DESC LIMIT 20""", (r["id"],))]}


@app.get("/api/people")
def people():
    d = db()
    since = (datetime.now() - timedelta(days=13)).strftime("%Y-%m-%d")
    out = []
    for r in d.execute("SELECT * FROM speakers WHERE merged_into IS NULL ORDER BY named DESC, name").fetchall():
        p = person_core(d, r)
        if not p["lines"] and not p["named"]:
            continue
        per = {x["day"]: round(x["secs"]) for x in d.execute(
            "SELECT day, SUM(dur) secs FROM segments WHERE speaker_id=? AND noise=0 AND day>=? GROUP BY day", (r["id"], since))}
        p["last14"] = [per.get((datetime.now() - timedelta(days=13 - i)).strftime("%Y-%m-%d"), 0) for i in range(14)]
        out.append(p)
    return out


@app.get("/api/people/{pid}")
def person(pid: int):
    d = db()
    r = d.execute("SELECT * FROM speakers WHERE id=?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404, "no such person")
    if r["merged_into"]:
        return {"merged_into": r["merged_into"]}
    p = person_core(d, r)
    p["per_day"] = [{"day": x["day"], "secs": round(x["secs"]), "lines": x["n"]} for x in d.execute(
        "SELECT day, SUM(dur) secs, COUNT(*) n FROM segments WHERE speaker_id=? AND noise=0 GROUP BY day ORDER BY day DESC LIMIT 60", (pid,))]
    p["recent"] = [row(x) for x in d.execute(SEG_SQL + " WHERE s.speaker_id=? AND s.noise=0 ORDER BY s.ts DESC LIMIT 40", (pid,))]
    p["action_items"] = [ai_row(d, x) for x in d.execute(
        "SELECT * FROM action_items WHERE lower(owner)=lower(?) ORDER BY sent, COALESCE(due,'9999'), source_ts DESC LIMIT 100", (r["name"],))]
    return p


@app.get("/api/search")
def search_all(q: str = "", speaker_id: int = 0, start: str = "", end: str = "", kinds: str = "lines,ideas,actions",
               limit: int = 300):
    """Word search with filters across lines, ideas and action items. The client highlights q."""
    d = db()
    ks = set(kinds.split(","))
    a = datetime.strptime(start, "%Y-%m-%d").timestamp() if start else None
    b = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).timestamp() if end else None
    like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    out = {"q": q, "lines": [], "ideas": [], "actions": []}
    if "lines" in ks and (q.strip() or speaker_id):
        where, args = ["s.noise=0"], []
        if q.strip():
            where.append("s.text LIKE ? ESCAPE '\\'"); args.append(like)
        if speaker_id:
            where.append("s.speaker_id=?"); args.append(speaker_id)
        if a:
            where.append("s.ts>=?"); args.append(a)
        if b:
            where.append("s.ts<?"); args.append(b)
        out["lines"] = [row(x) for x in d.execute(SEG_SQL + " WHERE " + " AND ".join(where) + " ORDER BY s.ts DESC LIMIT ?",
                                                    args + [max(1, min(limit, 2000))])]
    if "ideas" in ks and q.strip():
        for r in d.execute("SELECT * FROM ideas WHERE title LIKE ? ESCAPE '\\' OR id IN (SELECT idea_id FROM storyboards WHERE body LIKE ? ESCAPE '\\') "
                           "OR id IN (SELECT idea_id FROM idea_briefs WHERE body LIKE ? ESCAPE '\\') ORDER BY created DESC LIMIT 50", (like, like, like)):
            if (a and r["created"] < a) or (b and r["created"] >= b):
                continue
            out["ideas"].append({"id": r["id"], "title": r["title"], "status": r["status"], "created": r["created"]})
    if "actions" in ks and (q.strip() or speaker_id):
        where, args = [], []
        if q.strip():
            where.append("(task LIKE ? ESCAPE '\\' OR specifics LIKE ? ESCAPE '\\' OR owner LIKE ? ESCAPE '\\')"); args += [like, like, like]
        if speaker_id:
            nm = d.execute("SELECT name FROM speakers WHERE id=?", (speaker_id,)).fetchone()
            where.append("lower(owner)=lower(?)"); args.append(nm[0] if nm else "")
        if a:
            where.append("source_ts>=?"); args.append(a)
        if b:
            where.append("source_ts<?"); args.append(b)
        out["actions"] = [ai_row(d, x) for x in d.execute("SELECT * FROM action_items WHERE " + " AND ".join(where) +
                                                           " ORDER BY source_ts DESC LIMIT 200", args)]
    return out


@app.get("/api/onthisday")
def on_this_day(day: str = ""):
    """The same date 1 week, 1 month and 1 year earlier: recap headline, counts and a few lines."""
    base = datetime.strptime(day, "%Y-%m-%d") if day else datetime.now()
    def month_back(dt, n):
        y, m = dt.year, dt.month - n
        while m < 1:
            m += 12; y -= 1
        import calendar as _c
        return dt.replace(year=y, month=m, day=min(dt.day, _c.monthrange(y, m)[1]))
    targets = [("1 week ago", base - timedelta(days=7)), ("1 month ago", month_back(base, 1)), ("1 year ago", month_back(base, 12))]
    d = db()
    out = []
    for label, dt in targets:
        dd = dt.strftime("%Y-%m-%d")
        n = d.execute("SELECT COUNT(*), COALESCE(SUM(dur),0) FROM segments WHERE day=? AND noise=0", (dd,)).fetchone()
        sm = d.execute("SELECT body FROM summaries WHERE day=? ORDER BY version DESC LIMIT 1", (dd,)).fetchone()
        sample = [row(x) for x in d.execute(SEG_SQL + " WHERE s.day=? AND s.noise=0 AND length(s.text)>25 ORDER BY s.dur DESC LIMIT 3", (dd,))]
        out.append({"label": label, "day": dd, "lines": n[0], "talk_secs": round(n[1]),
                    "headline": json.loads(sm["body"]).get("headline") if sm else None, "sample": sample})
    return out

# ---- live push: unix datagram from capture/diarize -> in-process pub/sub -> SSE ----
class Hub:
    def __init__(self):
        self.subs = set()

    def publish(self, event, data, eid=None):
        msg = (f"id: {eid}\n" if eid else "") + f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
        for q in list(self.subs):
            try:
                q.put_nowait((event, data, msg))
            except asyncio.QueueFull:
                self.subs.discard(q)      # a stuck client is dropped ...
                q.dropped = True          # ... and its stream ends, so EventSource reconnects and the page reloads


hub = Hub()


class NotifyProto(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        try:
            m = json.loads(data)
            if m.get("t") == "idea":
                hub.publish("idea", {"id": m["id"]})
                return
            if m.get("t") == "cmd":
                r = db().execute("SELECT command_id, target, task, status, result, speaker FROM command_intents WHERE command_id=?", (m["id"],)).fetchone()
                if r:
                    hub.publish("command", dict(r))
                return
            if m.get("t") in ("live", "conv"):
                hub.publish(m["t"], {"id": m.get("id")})
                return
            if m.get("t") == "cap":
                hub.publish("status", status())
                return
            if m.get("t") == "day":
                hub.publish("summary", {"day": m["day"]})
                return
            ids = [m["id"]] if m.get("t") == "seg" else m.get("ids", [])
            if not ids:
                return
            ph = ",".join("?" * len(ids))
            rows = db().execute(SEG_SQL + f" WHERE s.id IN ({ph}) ORDER BY s.ts", ids).fetchall()
            now = time.time()
            for r in rows:
                d = row(r)
                d["lat"] = round(now - (r["ts"] + r["dur"]), 3) if m["t"] == "seg" else None
                hub.publish("seg" if m["t"] == "seg" else "upd", d, r["id"] if m["t"] == "seg" else None)
        except Exception:
            pass


# ---- B3-2 MCP (read-only) at /mcp: same bind and tailnet serve entry as the site, no new exposure ----
_mcp = {"app": None, "stack": None}


def _mount_mcp():
    try:
        from mcp.server.transport_security import TransportSecuritySettings
        from . import mcp_server
        srv = mcp_server.build_server()
        host = config.TAILNET_URL.split("://", 1)[-1]
        tail = [host, host.split(":")[0]] if host else []           # no tailnet URL configured: localhost only
        sec = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                        allowed_hosts=[f"127.0.0.1:{config.WEB_PORT}", f"localhost:{config.WEB_PORT}", *tail],
                                        allowed_origins=[f"http://127.0.0.1:{config.WEB_PORT}", f"http://localhost:{config.WEB_PORT}",
                                                         *([config.TAILNET_URL] if host else [])])
        sub = srv.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True, transport_security=sec)
        app.mount("/mcp", sub)
        _mcp["app"], _mcp["srv"] = sub, srv
    except Exception as e:
        print("MCP not mounted:", e)


_mount_mcp()


@app.on_event("startup")
async def start_mcp():
    import contextlib
    if _mcp.get("srv"):
        _mcp["stack"] = contextlib.AsyncExitStack()
        await _mcp["stack"].enter_async_context(_mcp["srv"].session_manager.run())


@app.on_event("shutdown")
async def stop_mcp():
    if _mcp.get("stack"):
        await _mcp["stack"].aclose()


@app.on_event("startup")
async def start_listener():
    path = str(config.NOTIFY_SOCK)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(path)
    os.chmod(path, 0o600)
    await asyncio.get_running_loop().create_datagram_endpoint(NotifyProto, sock=sock)
    asyncio.get_running_loop().create_task(_unload_loop())


async def _unload_loop():
    """Free the NPU embedder after it sits idle (only loaded by a semantic search or an ask)."""
    from . import search
    while True:
        await asyncio.sleep(60)
        search.unload_if_idle()


@app.get("/api/stream")
async def stream(request: Request):
    q = asyncio.Queue(maxsize=500)
    hub.subs.add(q)

    async def gen():
        try:
            yield "retry: 2000\n\n"
            yield "event: status\ndata: " + json.dumps(status()) + "\n\n"
            while not getattr(q, "dropped", False):
                try:
                    _, _, msg = await asyncio.wait_for(q.get(), timeout=10)
                    yield msg
                except asyncio.TimeoutError:
                    yield "event: status\ndata: " + json.dumps(status()) + "\n\n"   # also the keepalive
                if await request.is_disconnected():
                    break
        finally:
            hub.subs.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- ideas and storyboards ----
def idea_row(d, r, full=False):
    from . import ideatime
    keys = r.keys()
    extra = {"heat": r["heat"] if "heat" in keys else None, "mentions": r["mentions"] if "mentions" in keys else None,
             "last_mention": r["last_mention"] if "last_mention" in keys else None,
             "repo_url": r["repo_url"] if "repo_url" in keys else None, "shipped_at": r["shipped_at"] if "shipped_at" in keys else None,
             "lineage": ideatime.lineage(d, r["id"])}
    sbs = d.execute("SELECT id, version, status, created, generator, model, latency_s, error FROM storyboards WHERE idea_id=? ORDER BY version", (r["id"],)).fetchall()
    out = {"id": r["id"], "title": r["title"], "status": r["status"], "source": r["source"], "open": bool(r["open"]),
           "created": r["created"], "webhook_state": r["webhook_state"], "build_requested": bool(r["build_requested"]),
           "versions": [dict(x) for x in sbs], **extra}
    ls = d.execute("""SELECT s.id, s.ts, s.dur, s.day, s.text, s.noise, s.speaker_id, s.manual, s.diar, COALESCE(sp.name,'Unknown') AS speaker,
                      sp.color AS sp_color, COALESCE(sp.named,0) AS sp_named
                      FROM idea_lines il JOIN segments s ON s.id=il.segment_id LEFT JOIN speakers sp ON sp.id=s.speaker_id
                      WHERE il.idea_id=? ORDER BY s.ts, s.id""", (r["id"],)).fetchall()
    out["lines"] = [row(x) for x in ls]
    return out


def latest_body(d, idea_id, v=None):
    q = "SELECT * FROM storyboards WHERE idea_id=? AND status='ok'" + (" AND version=?" if v else "") + " ORDER BY version DESC LIMIT 1"
    r = d.execute(q, (idea_id, v) if v else (idea_id,)).fetchone()
    if not r:
        return None, None
    body = json.loads(r["body"])
    # the mockup HTML is served separately (sandboxed); the API only lists them
    body["mockups"] = [{"n": m["n"], "title": m["title"]} for m in body.get("mockups", [])]
    return r, body


def ask_worker(idea_id):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.sendto(json.dumps({"t": "build", "id": idea_id}).encode(), str(config.IDEAS_SOCK))
        s.close()
    except OSError:
        pass   # worker down: build_requested stays set, it builds on its next start or tick


BRIEF_KEYS = ("gist", "problem", "features", "open_questions", "quotes")


def derived_brief(body):
    """'Cleaned up' from the storyboard until Fred edits it."""
    if not body:
        return None
    f = body.get("features") or {}
    return {"gist": body.get("pitch", ""), "problem": (body.get("problem") or {}).get("text", ""),
            "features": list(f.get("mvp", [])) + list(f.get("later", [])), "open_questions": body.get("open_questions", []),
            "quotes": [q.get("text", "") if isinstance(q, dict) else str(q) for q in body.get("quotes", [])]}


def current_brief(d, iid, body):
    r = d.execute("SELECT version, body, who, created FROM idea_briefs WHERE idea_id=? ORDER BY version DESC LIMIT 1", (iid,)).fetchone()
    if r:
        return {**json.loads(r["body"]), "edited": True, "version": r["version"], "who": r["who"], "at": r["created"]}
    b = derived_brief(body)
    return {**b, "edited": False} if b else None


@app.post("/api/ideas/{iid}/brief")
async def save_brief(iid: int, request: Request):
    """Save the edited 'Cleaned up' brief as a new version (old versions are kept)."""
    body = await request.json()
    d = db()
    if not d.execute("SELECT 1 FROM ideas WHERE id=?", (iid,)).fetchone():
        raise HTTPException(404, "no such idea")
    clean = {}
    for k in BRIEF_KEYS:
        v = body.get(k, "" if k in ("gist", "problem") else [])
        if k in ("gist", "problem"):
            clean[k] = str(v)[:4000]
        else:
            items = v if isinstance(v, list) else str(v).split("\n")
            clean[k] = [" ".join(str(x).split())[:600] for x in items if str(x).strip()][:40]
    n = (d.execute("SELECT MAX(version) FROM idea_briefs WHERE idea_id=?", (iid,)).fetchone()[0] or 0) + 1
    d.execute("INSERT INTO idea_briefs(idea_id,version,body,who,created) VALUES(?,?,?,?,?)",
              (iid, n, json.dumps(clean), who_from(request), time.time()))
    if body.get("title"):
        d.execute("UPDATE ideas SET title=?, updated=? WHERE id=?", (" ".join(str(body["title"]).split())[:120], time.time(), iid))
    hub.publish("idea", {"id": iid})
    return {"ok": True, "version": n}


@app.get("/api/ideas/review")
def ideas_review(weeks: int = 4):
    """B4-7 weekly review: Later and Dismissed ideas from the last weeks, hottest first."""
    from . import ideatime
    return ideatime.review(db(), max(1, min(weeks, 26)))


@app.get("/api/ideas")
def list_ideas(v: int = 0):
    d = db()
    out = []
    for r in d.execute("SELECT * FROM ideas ORDER BY created DESC").fetchall():
        o = idea_row(d, r)
        sb, body = latest_body(d, r["id"])
        o["storyboard"] = {"version": sb["version"], "generator": sb["generator"], "model": sb["model"],
                           "latency_s": sb["latency_s"], "id": sb["id"], "body": body} if sb else None
        o["brief"] = current_brief(d, r["id"], body)
        out.append(o)
    return out


@app.get("/api/ideas/{iid}")
def get_idea(iid: int, v: int = 0):
    d = db()
    r = d.execute("SELECT * FROM ideas WHERE id=?", (iid,)).fetchone()
    if not r:
        raise HTTPException(404, "no such idea")
    o = idea_row(d, r)
    sb, body = latest_body(d, iid, v or None)
    o["storyboard"] = {"version": sb["version"], "generator": sb["generator"], "model": sb["model"],
                       "latency_s": sb["latency_s"], "id": sb["id"], "body": body} if sb else None
    o["brief"] = current_brief(d, iid, body)
    return o


@app.post("/api/ideas")
async def create_idea(request: Request):
    """Make this an idea: from explicit line ids or a time range. Idempotent: the same lines return the same idea."""
    body = await request.json()
    d = db()
    ids = body.get("segment_ids")
    if not ids:
        rs = segments(start=body.get("start", ""), end=body.get("end", ""), noise=0, media=0)
        ids = [x["id"] for x in rs]
    else:       # a selection that swept over a 'Media skipped' row: the TV lines stay out of the idea
        ph = ",".join("?" * len(ids))
        ids = [r[0] for r in d.execute(f"SELECT id FROM segments WHERE id IN ({ph}) AND COALESCE(media,0) NOT IN (1,2)",
                                       [int(i) for i in ids])]
    if not ids:
        raise HTTPException(400, "no lines in that selection")
    try:
        iid, created = ideasmod.create(d, ids, "range")
    except ValueError as e:
        raise HTTPException(400, str(e))
    if body.get("build"):
        _request_build(d, iid, rebuild=False)
    hub.publish("idea", {"id": iid})
    return {"ok": True, "idea_id": iid, "created": created}


def _request_build(d, iid, rebuild):
    last = d.execute("SELECT status FROM storyboards WHERE idea_id=? ORDER BY version DESC LIMIT 1", (iid,)).fetchone()
    if last and last["status"] == "building":
        return "building"
    if last and last["status"] == "ok" and not rebuild:
        return "exists"
    d.execute("UPDATE ideas SET build_requested=?, open=0 WHERE id=?", (2 if rebuild else 1, iid))
    ask_worker(iid)
    return "requested"


@app.post("/api/ideas/{iid}/build")
async def build_idea(iid: int, request: Request):
    body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    d = db()
    if not d.execute("SELECT 1 FROM ideas WHERE id=?", (iid,)).fetchone():
        raise HTTPException(404, "no such idea")
    state = _request_build(d, iid, bool(body.get("rebuild")))
    hub.publish("idea", {"id": iid})
    return {"ok": True, "state": state}


@app.post("/api/ideas/{iid}/shipped")
async def idea_shipped(iid: int, request: Request):
    """B4-9 shipped loop: a finished DO IT project flips its card to shipped with the repo link. Called by Larry
    Manager (server-to-server on the tailnet; if RAMBLR_SHIPPED_KEY is set it must be sent as a bearer token) or by
    Fred's click on the site."""
    import hmac
    key = os.environ.get("RAMBLR_SHIPPED_KEY", "")
    origin, host = request.headers.get("origin"), request.headers.get("host", "")
    same_site = bool(origin) and origin.split("://", 1)[-1] == host
    if key and not same_site:
        got = request.headers.get("authorization", "")[7:]
        if not hmac.compare_digest(got.strip(), key):
            raise HTTPException(401, "shipped key required")
    b = await request.json()
    url = str(b.get("repo_url", "")).strip()
    if url and not re.match(r"^https://[\w.-]+/[\w./-]+$", url):
        raise HTTPException(400, "repo_url must be an https URL")
    d = db()
    if d.execute("UPDATE ideas SET status='shipped', repo_url=COALESCE(NULLIF(?, ''), repo_url), shipped_at=?, updated=? WHERE id=?",
                 (url, time.time(), time.time(), iid)).rowcount == 0:
        raise HTTPException(404, "no such idea")
    hub.publish("idea", {"id": iid})
    return {"ok": True, "status": "shipped", "repo_url": url or None}


@app.post("/api/ideas/{iid}/xpost")
async def idea_xpost(iid: int):
    from . import drafts
    return await asyncio.to_thread(lambda: run_op(lambda: drafts.make_x_post(db(), idea_id=iid)))


@app.post("/api/xpost")
async def lines_xpost(request: Request):
    from . import drafts
    ids = (await request.json()).get("segment_ids") or []
    if not ids:
        raise HTTPException(400, "select lines first")
    return await asyncio.to_thread(lambda: run_op(lambda: drafts.make_x_post(db(), segment_ids=ids)))


@app.post("/api/ideas/{iid}/status")
async def idea_status(iid: int, request: Request):
    st = (await request.json()).get("status")
    if st not in ("new", "later", "dismissed"):
        raise HTTPException(400, "status must be new, later or dismissed")
    d = db()
    if d.execute("UPDATE ideas SET status=?, updated=? WHERE id=?", (st, time.time(), iid)).rowcount == 0:
        raise HTTPException(404, "no such idea")
    hub.publish("idea", {"id": iid})
    return {"ok": True}


@app.post("/api/ideas/{iid}/doit")
def idea_doit(iid: int):
    """DO IT: queue/send the idea to the webhook. Never automatic; idempotent on the idea id."""
    d = db()
    r = d.execute("SELECT * FROM ideas WHERE id=?", (iid,)).fetchone()
    if not r:
        raise HTTPException(404, "no such idea")
    o = idea_row(d, r)
    sb, body = latest_body(d, iid)
    brief = current_brief(d, iid, body) or {}
    res = doit.send(d, iid, brief.get("gist") or (body or {}).get("pitch") or r["title"], brief,
                    [{"ts": l["ts"], "speaker": l["speaker"], "text": l["text"]} for l in o["lines"]], storyboard=body)
    state = {"sent": "sent", "already sent": "sent", "pending webhook": "pending webhook"}.get(res, "failed")
    d.execute("UPDATE ideas SET webhook_state=?, status=CASE WHEN ?='sent' THEN 'done-it' ELSE status END, updated=? WHERE id=?",
              (state, state, time.time(), iid))
    hub.publish("idea", {"id": iid})
    return {"ok": True, "result": res, "webhook_state": state}


@app.get("/api/storyboards/{sid}/mockups/{n}")
def mockup_html(sid: int, n: int):
    r = db().execute("SELECT body FROM storyboards WHERE id=? AND status='ok'", (sid,)).fetchone()
    ms = json.loads(r["body"]).get("mockups", []) if r else []
    m = next((x for x in ms if x["n"] == n), None)
    if not m:
        raise HTTPException(404)
    return HTMLResponse(m["html"], headers={"Content-Security-Policy": mockmod.CSP + "; sandbox", "X-Content-Type-Options": "nosniff"})


@app.get("/api/storyboards/{sid}/mockups/{n}/thumb.png")
def mockup_png(sid: int, n: int):
    p = mockmod.thumb_path(sid, n)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/png")


@app.get("/api/storyboards/{sid}/panels/{n}.png")
def panel_png(sid: int, n: int):
    from . import panelart
    p = panelart.panel_path(sid, n)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/png")


@app.get("/calendar")
def calendar_page():
    return FileResponse(HERE / "static" / "calendar.html", media_type="text/html")


@app.get("/people")
def people_page():
    return FileResponse(HERE / "static" / "people.html", media_type="text/html")


@app.get("/search")
def search_page():
    return FileResponse(HERE / "static" / "search.html", media_type="text/html")


@app.get("/settings")
def settings_page():
    return FileResponse(HERE / "static" / "settings.html", media_type="text/html")


@app.get("/static/{name}")
def static_file(name: str):
    allowed = {"palette.js": "text/javascript", "app.js": "text/javascript", "theme.css": "text/css"}
    if name not in allowed:
        raise HTTPException(404)
    return FileResponse(HERE / "static" / name, media_type=allowed[name])


@app.get("/ideas")
def ideas_page():
    return FileResponse(HERE / "static" / "ideas.html", media_type="text/html")

@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html", media_type="text/html")


@app.get("/favicon.ico")
def fav_ico():
    return FileResponse(BRAND / "favicon.ico", media_type="image/x-icon")


@app.get("/brand/{name}")
def brand(name: str):
    allowed = {"nixfred-icon.svg": "image/svg+xml", "nixfred-favicon.svg": "image/svg+xml",
               "favicon-32.png": "image/png", "favicon-16.png": "image/png", "apple-touch-icon.png": "image/png"}
    if name not in allowed:
        raise HTTPException(404)
    return FileResponse(BRAND / name, media_type=allowed[name])


def main():
    import uvicorn
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="warning", access_log=False,
                timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
