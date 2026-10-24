"""Who's in the room (B2-4), from the voices Ramblr hears, plus quiet hours (flag only).

Presence is inferred, never sensed: a person counts as in the room from PAD seconds before a line of theirs to
PAD seconds after it, and two stretches merge when the silence between them is shorter than MERGE (people sit
quietly). Only diarized room lines count: TV/music lines (media) and noise never make anyone present. Unnamed
voices are pooled as "Unknown voices". Nothing here changes recording; quiet hours only flag lines and shade the
timeline."""
import os
import time
from datetime import datetime

from . import config, speakers, state

PAD = float(os.environ.get("RAMBLR_PRESENCE_PAD", 300))       # 5 min either side of a line
MERGE = float(os.environ.get("RAMBLR_PRESENCE_MERGE", 1200))  # silences under 20 min keep a person present
UNKNOWN = "unknown"


def intervals(db, a, b):
    """{person_key: {name, color, spans: [[t0, t1]], lines, talk}} for lines with a < ts < b."""
    rows = db.execute("""SELECT s.ts, s.dur, s.speaker_id, sp.name, sp.named, sp.color FROM segments s
                         LEFT JOIN speakers sp ON sp.id=s.speaker_id
                         WHERE s.ts>=? AND s.ts<? AND s.noise=0 AND (s.diar=1 OR s.manual=1) AND COALESCE(s.media,0) NOT IN (1,2)
                         AND COALESCE(s.source,'') NOT IN ('Zoom','Wispr')
                         ORDER BY s.ts""", (a - PAD, b + PAD)).fetchall()
    people = {}
    for r in rows:
        named = bool(r["named"])
        key = str(r["speaker_id"]) if named else UNKNOWN
        p = people.setdefault(key, {"key": key, "speaker_id": r["speaker_id"] if named else None,
                                    "name": r["name"] if named else "Unknown voices",
                                    "color": speakers.color_for(r["speaker_id"], 1, r["color"]) if named else speakers.GRAY,
                                    "spans": [], "lines": 0, "talk": 0.0})
        t0, t1 = r["ts"] - PAD, r["ts"] + r["dur"] + PAD
        if p["spans"] and t0 - p["spans"][-1][1] < MERGE - 2 * PAD:
            p["spans"][-1][1] = max(p["spans"][-1][1], t1)
        else:
            p["spans"].append([t0, t1])
        if a <= r["ts"] < b:
            p["lines"] += 1
            p["talk"] += r["dur"]
    for p in people.values():
        p["spans"] = [[max(s[0], a), min(s[1], b, time.time())] for s in p["spans"] if s[1] > a and s[0] < b]
        p["present_secs"] = round(sum(s[1] - s[0] for s in p["spans"]))
        p["talk"] = round(p["talk"])
    order = sorted(people.values(), key=lambda p: (p["key"] == UNKNOWN, -p["present_secs"]))
    return [p for p in order if p["spans"]]


def present_now(db, now=None):
    now = now or time.time()
    return [{"name": p["name"], "color": p["color"], "since": p["spans"][-1][0]}
            for p in intervals(db, now - 6 * 3600, now) if p["spans"][-1][1] >= now - 1]


# ---- quiet hours: flag only ----
DEFAULT_QUIET = {"enabled": True, "start": "23:00", "end": "07:00"}


_qc = {"m": None, "cfg": None}


def quiet_cfg():
    m = state.mtime()          # cached until state.json changes: row() asks once per transcript line
    if _qc["m"] != m:
        _qc.update(m=m, cfg={**DEFAULT_QUIET, **(state.load().get("quiet") or {})})
    return _qc["cfg"]


def _mins(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_quiet(ts, cfg=None):
    cfg = cfg or quiet_cfg()
    if not cfg.get("enabled"):
        return False
    t = datetime.fromtimestamp(ts)
    m, s, e = t.hour * 60 + t.minute, _mins(cfg["start"]), _mins(cfg["end"])
    return (s <= m < e) if s < e else (m >= s or m < e)


def quiet_spans(day, cfg=None):
    """Quiet-hour spans that fall inside one local day, as [[t0, t1]] epoch seconds."""
    cfg = cfg or quiet_cfg()
    if not cfg.get("enabled"):
        return []
    d0 = datetime.strptime(day, "%Y-%m-%d").timestamp()
    s, e = _mins(cfg["start"]) * 60, _mins(cfg["end"]) * 60
    if s < e:
        return [[d0 + s, d0 + e]]
    return [[d0, d0 + e], [d0 + s, d0 + 86400]]


def set_quiet(enabled, start, end, by="site"):
    for v in (start, end):
        h, m = v.split(":")
        if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError("times must be HH:MM")
    return state.update(by=by, quiet={"enabled": bool(enabled), "start": start, "end": end})["quiet"]
