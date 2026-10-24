"""Google Calendar overlay for Fred's Google account, read-only (B4-6; Fred 4:07 AM: "Overlay google calendar
Fred's Google account").

Credential: the calendar's private iCal address (Google Calendar > Settings > the calendar > "Secret address in
iCal format"), pasted by Fred into ~/.config/ramblr/secrets.env as RAMBLR_GCAL_ICS_URL. It is read-only by nature,
never printed or logged. Until it exists the overlay is a stub (BLOCKED.md). Fetched at most every 15 minutes;
events are cached in calendar_events (upserted, never deleted) and drawn on the Today timeline, the morning brief
and the context tags. Recurring events (RRULE FREQ=DAILY/WEEKLY with BYDAY/COUNT/UNTIL/INTERVAL) are expanded 60
days back and ahead; anything fancier keeps its first occurrence."""
import logging
import os
import re
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("ramblr.calendar")
REFRESH = 900
DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def url():
    return os.environ.get("RAMBLR_GCAL_ICS_URL", "")


def status():
    return "ready" if url().startswith("https://") else "stub: paste the calendar's secret iCal address as RAMBLR_GCAL_ICS_URL"


def _unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text).replace("\r", "")


def _dt(value, params):
    v = value.strip()
    if "VALUE=DATE" in params or re.fullmatch(r"\d{8}", v):
        d = datetime.strptime(v[:8], "%Y%m%d")
        return d.timestamp(), True
    if v.endswith("Z"):
        return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp(), False
    tz = None
    m = re.search(r"TZID=([^;:]+)", params)
    if m:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(m.group(1))
        except Exception:
            tz = None
    d = datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
    return (d.replace(tzinfo=tz).timestamp() if tz else d.timestamp()), False


def parse(text, now=None):
    """[{uid, start, end, all_day, title, location, status}] with simple recurrences expanded."""
    now = now or time.time()
    lo, hi = now - 60 * 86400, now + 60 * 86400
    out = []
    for block in re.findall(r"BEGIN:VEVENT\n(.*?)\nEND:VEVENT", _unfold(text), re.S):
        props = {}
        for line in block.split("\n"):
            if ":" not in line:
                continue
            head, val = line.split(":", 1)
            name, _, params = head.partition(";")
            props.setdefault(name.upper(), (val, params))
        if "DTSTART" not in props:
            continue
        start, all_day = _dt(*props["DTSTART"])
        end = _dt(*props["DTEND"])[0] if "DTEND" in props else start + (86400 if all_day else 3600)
        ev = {"uid": props.get("UID", ("", ""))[0], "start": start, "end": end, "all_day": all_day,
              "title": props.get("SUMMARY", ("(busy)", ""))[0].replace("\\,", ",").replace("\\n", " ")[:200],
              "location": props.get("LOCATION", ("", ""))[0].replace("\\,", ",")[:200],
              "status": props.get("STATUS", ("CONFIRMED", ""))[0].upper()}
        rule = props.get("RRULE", ("", ""))[0]
        if not rule:
            if hi >= start and end >= lo:
                out.append(ev)
            continue
        r = dict(kv.split("=", 1) for kv in rule.split(";") if "=" in kv)
        freq, interval = r.get("FREQ"), int(r.get("INTERVAL", "1"))
        count = int(r["COUNT"]) if "COUNT" in r else None
        until = _dt(r["UNTIL"], "")[0] if "UNTIL" in r else hi
        dur = end - start
        if freq not in ("DAILY", "WEEKLY"):
            out.append(ev)
            continue
        byday = [DAYS[d[-2:]] for d in r.get("BYDAY", "").split(",") if d[-2:] in DAYS]
        first = datetime.fromtimestamp(start)
        n, k = 0, 0
        while k < 800:
            day = first + timedelta(days=k)
            k += 1
            weeks = (day.date() - first.date()).days // 7
            if freq == "DAILY" and (k - 1) % interval:
                continue
            if freq == "WEEKLY" and (weeks % interval or day.weekday() not in (byday or [first.weekday()])):
                continue
            t0 = day.timestamp()
            if t0 > until or t0 > hi or (count and n >= count):
                break
            n += 1
            if t0 + dur >= lo:
                out.append({**ev, "uid": f"{ev['uid']}@{int(t0)}", "start": t0, "end": t0 + dur})
    return out


def refresh(db, force=False):
    """Fetch and cache (rate limited). Returns a short status dict; never raises for network trouble."""
    if not url().startswith("https://"):
        return {"status": status()}
    from . import db as dbm
    last = float(dbm.get_meta(db, "gcal_fetched", "0") or 0)
    if not force and time.time() - last < REFRESH:
        return {"status": "cached"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url(), headers={"User-Agent": "ramblr"}), timeout=20) as r:
            text = r.read(5_000_000).decode("utf-8", errors="replace")
    except Exception as e:
        dbm.set_meta(db, "gcal_error", type(e).__name__)      # never the URL
        return {"status": f"fetch failed: {type(e).__name__}"}
    evs = parse(text)
    for e in evs:
        db.execute("""INSERT INTO calendar_events(uid, start, end, all_day, title, location, status, updated) VALUES(?,?,?,?,?,?,?,?)
                      ON CONFLICT(uid) DO UPDATE SET start=excluded.start, end=excluded.end, all_day=excluded.all_day, title=excluded.title,
                      location=excluded.location, status=excluded.status, updated=excluded.updated""",
                   (e["uid"], e["start"], e["end"], 1 if e["all_day"] else 0, e["title"], e["location"], e["status"], time.time()))
    dbm.set_meta(db, "gcal_fetched", time.time())
    dbm.set_meta(db, "gcal_error", "")
    return {"status": "ok", "events": len(evs)}


def between(db, a, b):
    return [dict(r) for r in db.execute("""SELECT uid, start, end, all_day, title, location FROM calendar_events
                                           WHERE start<? AND end>? AND status!='CANCELLED' ORDER BY start""", (b, a))]
