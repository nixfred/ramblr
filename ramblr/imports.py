"""Zoom and Wispr imports onto the SAME timeline as the room (B4-1; Fred 4:07 AM: "Zoom and wispr import along side
what was recorded"). Imported lines are ordinary segments with source 'Zoom' or 'Wispr', interleaved by their real
time and tagged by source; they are not room audio, so they never feed media detection or room conversations.

Files work today, no credentials needed:
- Zoom: the .vtt transcript Zoom saves (cloud recording download or local recording with "audio transcript").
  Speaker names come from the cues ("Fred Nix: ..."); the meeting start comes from the request or the file name.
- Wispr Flow: an export of the dictation history as JSON ([{"text", "timestamp"|"created_at", "app"?}]) or CSV
  (text,timestamp[,app]).
Drop files into ~/.local/share/ramblr/imports/{zoom,wispr}/ (picked up by ramblr-ideas) or upload on /imports.

API pulls (Zoom Server-to-Server OAuth; Wispr has no public API) are stubbed until Fred supplies credentials
(RAMBLR_ZOOM_ACCOUNT_ID / RAMBLR_ZOOM_CLIENT_ID / RAMBLR_ZOOM_CLIENT_SECRET in secrets.env); see BLOCKED.md.
Each file is imported once (content hash); each line has a stable key, so re-imports never duplicate."""
import csv
import hashlib
import io
import json
import os
import re
import time
from datetime import datetime

from . import config, db as dbm

INBOX = config.DATA / "imports"
SOURCES = ("Zoom", "Wispr")


def _speaker(db, name):
    name = " ".join(name.split())[:60]
    if not name:
        return None
    r = db.execute("SELECT id FROM speakers WHERE lower(name)=lower(?) AND merged_into IS NULL", (name,)).fetchone()
    return r[0] if r else dbm.new_speaker(db, name, named=1)


def _ts(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) or re.fullmatch(r"\d{9,13}(\.\d+)?", str(v)):
        f = float(v)
        return f / 1000 if f > 1e11 else f
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_vtt(text):
    """[(start_s, end_s, speaker, text)] from WebVTT."""
    out = []
    blocks = re.split(r"\n\s*\n", text.replace("\r", ""))
    for b in blocks:
        m = re.search(r"(\d+:)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d+:)?(\d{2}):(\d{2})[.,](\d{3})", b)
        if not m:
            continue
        def secs(h, mi, s, ms):
            return int((h or "0:")[:-1] or 0) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000
        a = secs(m.group(1), m.group(2), m.group(3), m.group(4))
        z = secs(m.group(5), m.group(6), m.group(7), m.group(8))
        body = " ".join(l.strip() for l in b[m.end():].strip().splitlines() if l.strip())
        body = re.sub(r"<[^>]+>", "", body)
        spk, txt = "", body
        sm = re.match(r"^([^:]{1,60}):\s+(.+)$", body)
        if sm:
            spk, txt = sm.group(1).strip(), sm.group(2).strip()
        else:
            vm = re.search(r"<v\s+([^>]+)>", b)
            if vm:
                spk = vm.group(1).strip()
        if txt:
            out.append((a, z, spk, txt))
    return out


def meeting_start(name, given=None):
    if given:
        return given
    m = re.search(r"(\d{4})[-_]?(\d{2})[-_]?(\d{2})[ T_-]?(\d{2})[.:_-]?(\d{2})(?:[.:_-]?(\d{2}))?", name)
    if m:
        return datetime(*[int(x) for x in m.groups()[:5]], int(m.group(6) or 0)).timestamp()
    return None


def _insert(db, key, ts, dur, text, source, speaker_id):
    if db.execute("SELECT 1 FROM import_lines WHERE key=?", (key,)).fetchone():
        return None
    sid, _ = dbm.insert_segment(db, ts, max(dur, 0.5), text, None, f"import:{source}", 0, source=source)
    db.execute("UPDATE segments SET speaker_id=?, manual=?, diar=2 WHERE id=?", (speaker_id, 1 if speaker_id else 0, sid))
    db.execute("INSERT INTO import_lines(key, segment_id, created) VALUES(?,?,?)", (key, sid, time.time()))
    return sid


def import_zoom(db, name, text, start=None):
    start = meeting_start(name, start)
    if start is None:
        raise ValueError("no meeting start time: name the file like zoom_2026-10-24_09-30.vtt or pass start=")
    cues = parse_vtt(text)
    if not cues:
        raise ValueError("no transcript cues found in that file")
    fh = hashlib.sha1(text.encode()).hexdigest()[:16]
    ids = []
    for i, (a, z, spk, txt) in enumerate(cues):
        sid = _insert(db, f"zoom:{fh}:{i}", start + a, z - a, txt, "Zoom", _speaker(db, spk) if spk else None)
        if sid:
            ids.append(sid)
    return {"source": "Zoom", "lines": len(ids), "cues": len(cues), "start": start}


def import_wispr(db, name, text):
    items = []
    t = text.strip()
    if t.startswith("[") or t.startswith("{"):
        data = json.loads(t)
        data = data.get("items") or data.get("history") or data.get("dictations") if isinstance(data, dict) else data
        for d in data or []:
            items.append((d.get("text") or d.get("transcript") or "", _ts(d.get("timestamp") or d.get("created_at") or d.get("time")), d.get("app") or ""))
    else:
        for row in csv.DictReader(io.StringIO(t)):
            row = {k.lower().strip(): v for k, v in row.items() if k}
            items.append((row.get("text") or row.get("transcript") or "", _ts(row.get("timestamp") or row.get("created_at") or row.get("time")), row.get("app") or ""))
    me = db.execute("SELECT id FROM speakers WHERE confirmed=1 AND merged_into IS NULL ORDER BY id LIMIT 1").fetchone()
    ids = []
    for txt, ts, app in items:
        txt = " ".join(str(txt).split())
        if not txt or not ts:
            continue
        key = "wispr:" + hashlib.sha1(f"{ts}|{txt}".encode()).hexdigest()[:20]
        sid = _insert(db, key, ts, max(2.0, len(txt.split()) / 2.5), txt + (f"  [{app}]" if app else ""), "Wispr", me[0] if me else None)
        if sid:
            ids.append(sid)
    if not items:
        raise ValueError("no dictations found (expected JSON list or CSV with text,timestamp)")
    return {"source": "Wispr", "lines": len(ids), "items": len(items)}


def import_file(db, kind, name, text, start=None):
    h = hashlib.sha1(text.encode()).hexdigest()
    prev = db.execute("SELECT result FROM imports WHERE hash=?", (h,)).fetchone()
    if prev:
        return {**json.loads(prev["result"]), "already": True}
    res = import_zoom(db, name, text, start) if kind == "zoom" else import_wispr(db, name, text)
    db.execute("INSERT INTO imports(hash, kind, name, result, created) VALUES(?,?,?,?,?)", (h, kind, name[:200], json.dumps(res), time.time()))
    return res


def scan_inbox(db):
    """Files dropped into ~/.local/share/ramblr/imports/zoom|wispr are imported once, then moved to done/."""
    out = []
    for kind in ("zoom", "wispr"):
        d = INBOX / kind
        if not d.exists():
            continue
        for p in sorted(d.iterdir()):
            if not p.is_file() or p.suffix.lower() not in (".vtt", ".txt", ".json", ".csv"):
                continue
            try:
                res = import_file(db, kind, p.name, p.read_text(errors="replace"))
            except Exception as e:
                res = {"error": str(e)[:200]}
            done = d / "done"
            done.mkdir(exist_ok=True)
            p.rename(done / p.name)          # kept, just moved out of the inbox
            out.append({"file": p.name, **res})
    return out


def zoom_api_status():
    need = ("RAMBLR_ZOOM_ACCOUNT_ID", "RAMBLR_ZOOM_CLIENT_ID", "RAMBLR_ZOOM_CLIENT_SECRET")
    return "stub: Zoom API credentials not set (file import works)" if not all(os.environ.get(k) for k in need) else "credentials present (API pull not built yet)"
