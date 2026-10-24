"""Ambient context tags (B4-13): every 5 minutes, what was going on around the talk: the active window on gus
(app class and a short title), the calendar event at that moment (B4-6) and, if Fred sets a location, the weather.

- Active window: `hyprctl -i 0 activewindow -j` (local, read-only). Titles are cut to 60 characters.
- Weather: OFF unless RAMBLR_WEATHER_LATLON="lat,lon" is set in config.env; then Open-Meteo (free, no key; only the
  coordinates are sent). Never Fred's address, never transcript text.
- Snapshots are numbers and labels only, kept forever; lines show the nearest one in their tooltip."""
import json
import os
import subprocess
import time
import urllib.request

EVERY = 300


def window():
    try:
        p = subprocess.run(["hyprctl", "-i", "0", "activewindow", "-j"], capture_output=True, text=True, timeout=3,
                           env={k: v for k, v in os.environ.items() if k != "HYPRLAND_INSTANCE_SIGNATURE"})
        d = json.loads(p.stdout or "{}")
        return (d.get("class") or "")[:60], (d.get("title") or "")[:60]
    except Exception:
        return "", ""


WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog", 51: "drizzle", 53: "drizzle", 55: "drizzle",
       61: "rain", 63: "rain", 65: "heavy rain", 71: "snow", 73: "snow", 75: "heavy snow", 80: "showers", 81: "showers", 82: "heavy showers",
       95: "thunderstorm", 96: "thunderstorm", 99: "thunderstorm"}


def weather():
    ll = os.environ.get("RAMBLR_WEATHER_LATLON", "")
    if "," not in ll:
        return None
    lat, lon = [x.strip() for x in ll.split(",", 1)]
    try:
        float(lat), float(lon)
        u = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,weather_code&temperature_unit=fahrenheit"
        with urllib.request.urlopen(u, timeout=10) as r:
            c = json.loads(r.read()).get("current", {})
        return f"{round(c.get('temperature_2m', 0))}°F {WMO.get(c.get('weather_code'), '')}".strip()
    except Exception:
        return None


def snapshot(db, now=None):
    from . import calendar_feed, db as dbm
    now = now or time.time()
    last = float(dbm.get_meta(db, "ambient_last", "0") or 0)
    if now - last < EVERY:
        return None
    cls, title = window()
    ev = calendar_feed.between(db, now, now + 1)
    w = None
    if now - float(dbm.get_meta(db, "weather_last", "0") or 0) > 1800:
        w = weather()
        if w:
            dbm.set_meta(db, "weather_last", now)
            dbm.set_meta(db, "weather_now", w)
    w = w or (dbm.get_meta(db, "weather_now") or None)
    db.execute("INSERT INTO ambient(ts, window_class, window_title, event, weather) VALUES(?,?,?,?,?)",
               (now, cls, title, ev[0]["title"] if ev else None, w))
    dbm.set_meta(db, "ambient_last", now)
    return {"window": cls, "event": ev[0]["title"] if ev else None, "weather": w}


def for_day(db, a, b):
    return [dict(r) for r in db.execute("SELECT ts, window_class, window_title, event, weather FROM ambient WHERE ts>=? AND ts<? ORDER BY ts", (a, b))]
