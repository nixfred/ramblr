"""Player sources for the media log (B2-2): what was playing, from the player itself, not from the mic.

Pluggable: each Source says whether it is configured and returns what is playing now.
- MPRIS: every media player on gus's session bus (OmaSpotify, browsers, mpv...) through busctl.
- Cast (Fred, 2026-10-24 7:00 AM: "Google TV / Chromecast"): Google TV / Chromecast devices on the LAN, found with
  zeroconf (mDNS) and read with pychromecast in the project venv. No cloud account. Status and media metadata
  only: Ramblr never calls play, pause, volume, launch or any other control. No device = nothing, quietly.
  RAMBLR_CAST=0 turns it off; RAMBLR_CAST_ROOM="Living Room TV,..." limits which devices count for media
  detection (unset = every device found). Cast plays also feed media detection (media.features "cast").
Plex, Jellyfin, smart TV and the Spotify Web API stay stubs. Read-only everywhere; nothing is sent to a player."""
import json
import logging
import os
import subprocess
import threading
import time

log = logging.getLogger("ramblr.players")


class Source:
    name = "base"
    needs = ""

    def configured(self):
        return False

    def poll(self):
        """[{player, title, artist, album, url, status}] for everything playing right now."""
        return []

    def describe(self):
        return {"name": self.name, "configured": self.configured(), "needs": "" if self.configured() else self.needs}


class Mpris(Source):
    name = "mpris"
    needs = "busctl (systemd) and a user session bus"

    def configured(self):
        return bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS") or os.path.exists(f"/run/user/{os.getuid()}/bus"))

    def _bus(self, *args):
        p = subprocess.run(["busctl", "--user", "--json=short", *args], capture_output=True, text=True, timeout=5)
        return json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else None

    def poll(self):
        out = []
        names = subprocess.run(["busctl", "--user", "list", "--no-pager", "--no-legend"], capture_output=True, text=True, timeout=5).stdout
        for line in names.splitlines():
            bus = line.split()[0] if line.split() else ""
            if not bus.startswith("org.mpris.MediaPlayer2."):
                continue
            st = self._bus("get-property", bus, "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player", "PlaybackStatus")
            if not st or st.get("data") != "Playing":
                continue
            md = (self._bus("get-property", bus, "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player", "Metadata") or {}).get("data", {})
            val = lambda k: (md.get(k) or {}).get("data")
            artist = val("xesam:artist")
            out.append({"player": bus.removeprefix("org.mpris.MediaPlayer2.").split(".")[0], "title": val("xesam:title") or "",
                        "artist": ", ".join(artist) if isinstance(artist, list) else (artist or ""), "album": val("xesam:album") or "",
                        "url": val("xesam:url") or "", "status": "Playing"})
        return out


class Cast(Source):
    """Google TV / Chromecast now-playing over the LAN. A zeroconf browser and one status connection per device
    live in background threads inside the ideas worker, so poll() only reads cached status."""
    name = "cast"
    needs = "pychromecast in the project venv and a Cast device on the LAN"

    def __init__(self):
        self._lock = threading.Lock()
        self._casts = {}
        self._browser = None
        self._failed = ""

    def configured(self):
        if os.environ.get("RAMBLR_CAST", "1") == "0":
            return False
        try:
            import pychromecast  # noqa: F401
            return True
        except ImportError:
            return False

    def _start(self):
        if self._browser or self._failed:
            return
        try:
            import pychromecast
            import zeroconf
            from pychromecast.discovery import CastBrowser, SimpleCastListener
            zconf = zeroconf.Zeroconf()

            def add(uuid, _service):
                info = self._browser.devices.get(uuid)
                if not info:
                    return
                with self._lock:
                    if uuid in self._casts:
                        return
                    cc = pychromecast.get_chromecast_from_cast_info(info, zconf, tries=None, retry_wait=30)
                    cc.start()                      # status connection only; no command is ever sent
                    self._casts[uuid] = cc
                log.info("cast device found: %s (%s)", info.friendly_name, info.model_name)

            def remove(uuid, _service, _info):
                with self._lock:
                    cc = self._casts.pop(uuid, None)
                if cc:
                    cc.disconnect(blocking=False)

            self._browser = CastBrowser(SimpleCastListener(add, remove), zconf)
            self._browser.start_discovery()
        except Exception as e:                       # no multicast, no network: quietly nothing
            self._failed = f"{type(e).__name__}: {e}"[:200]
            log.warning("cast discovery unavailable: %s", self._failed)

    def devices(self):
        with self._lock:
            casts = list(self._casts.values())
        out = []
        for cc in casts:
            st, ms = cc.status, cc.media_controller.status
            out.append({"name": cc.cast_info.friendly_name, "model": cc.cast_info.model_name, "host": cc.cast_info.host,
                        "app": (st.display_name if st else None) or "", "idle": bool(cc.is_idle),
                        "player_state": ms.player_state if ms else "UNKNOWN"})
        return out

    def poll(self):
        self._start()
        out = []
        with self._lock:
            casts = list(self._casts.values())
        for cc in casts:
            try:
                st, ms = cc.status, cc.media_controller.status
                if not st or cc.is_idle or not ms or ms.player_state not in ("PLAYING", "BUFFERING"):
                    continue
                app = st.display_name or "Cast"
                title = ms.title or ms.series_title or f"({app})"
                artist = ms.artist or (ms.series_title if ms.title and ms.series_title else "") or ""
                album = ms.album_name or (f"S{ms.season}E{ms.episode}" if ms.season and ms.episode else "")
                out.append({"player": f"{cc.cast_info.friendly_name} · {app}", "title": title, "artist": artist,
                            "album": album or "", "url": "", "status": "Playing"})
            except Exception:
                continue
        return out


def cast_room():
    return [n.strip() for n in os.environ.get("RAMBLR_CAST_ROOM", "").split(",") if n.strip()]


def cast_playing(db, a, b, slack=30):
    """True if a Cast device that counts for this room was playing during [a, b] (plays are polled every 30 s)."""
    rows = db.execute("""SELECT player FROM media_plays WHERE source='cast' AND started<=? AND COALESCE(ended, last_seen)>=?""",
                      (b + slack, a - slack)).fetchall()
    room = cast_room()
    return any(not room or r["player"].split(" · ")[0] in room for r in rows)


class Stub(Source):
    def __init__(self, name, needs):
        self.name, self.needs = name, needs


SOURCES = [
    Mpris(),
    Cast(),
    Stub("plex", "RAMBLR_PLEX_URL + RAMBLR_PLEX_TOKEN in ~/.config/ramblr/secrets.env (not used: the TV is Google TV, read via Cast)"),
    Stub("jellyfin", "RAMBLR_JELLYFIN_URL + RAMBLR_JELLYFIN_KEY in secrets.env (not used)"),
    Stub("spotify-web", "a Spotify Web API app + refresh token in secrets.env (not used; local Spotify shows via MPRIS, Spotify on the TV via Cast)"),
]


def poll_all(db, now=None, stale=90):
    """Record plays: same item still playing extends its row; anything not seen for `stale` s is closed."""
    now = now or time.time()
    seen = set()
    for src in SOURCES:
        if not src.configured():
            continue
        try:
            items = src.poll()
        except Exception:
            continue
        for it in items:
            if not (it["title"] or it["artist"]):
                continue
            key = (src.name, it["player"], it["title"], it["artist"])
            r = db.execute("""SELECT id FROM media_plays WHERE source=? AND player=? AND title=? AND artist=? AND ended IS NULL
                              AND last_seen>=? ORDER BY id DESC LIMIT 1""", (*key, now - stale)).fetchone()
            if r:
                db.execute("UPDATE media_plays SET last_seen=? WHERE id=?", (now, r[0]))
                seen.add(r[0])
            else:
                cur = db.execute("""INSERT INTO media_plays(source,player,title,artist,album,url,started,last_seen)
                                    VALUES(?,?,?,?,?,?,?,?)""", (*key, it["album"], it["url"][:500], now, now))
                seen.add(cur.lastrowid)
    for src in SOURCES:                        # what Cast discovery sees, for the media page (the web runs no discovery)
        if isinstance(src, Cast) and src._browser:
            from . import db as dbm
            devs = json.dumps([{k: d[k] for k in ("name", "model", "app", "idle", "player_state")} for d in src.devices()])
            if dbm.get_meta(db, "cast_devices") != devs:
                dbm.set_meta(db, "cast_devices", devs)
    for r in db.execute("SELECT id, last_seen FROM media_plays WHERE ended IS NULL").fetchall():
        if r["id"] not in seen and now - r["last_seen"] > stale:
            db.execute("UPDATE media_plays SET ended=last_seen WHERE id=?", (r["id"],))
    return len(seen)
