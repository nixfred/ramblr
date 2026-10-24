"""Ramblr MCP server (B3-2): read-only tools over the transcript DB for AI clients.

Transports: stdio (`python -m ramblr.mcp_server`, for Claude Code on gus) and streamable HTTP mounted by the
web app at /mcp, so it is reachable only where the site is: 127.0.0.1:8798 and the existing tailnet serve entry.
Never public, never funnel, no new serve entry.

Security (B3-2 review):
- The DB is opened read-only (sqlite URI mode=ro) AND an authorizer refuses every write, DDL, ATTACH, PRAGMA
  writes and any function that is not read-only, so even a bug cannot change data.
- There is no raw-SQL tool. Every tool runs fixed, parameterised queries; text search goes through FTS5 with
  the query reduced to quoted word tokens. Inputs are clamped (limits, dates validated).
- Tools never return audio, file paths, secrets, config or keys; TV/music lines are left out unless asked for.
- Model calls (Ask) are not exposed: an MCP client is a model itself, and Ramblr spends nothing for it."""
import re
import sqlite3
from datetime import datetime, timedelta

from . import config

RO_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
LIVE_URL = config.TAILNET_URL


def _authorizer(action, arg1, arg2, dbname, source):
    if action in RO_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1 in ("data_version", "table_info", "query_only") and arg2 is None:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def ro_connect(path=None):
    p = str(path or config.DB_PATH)
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=1")
    db.set_authorizer(_authorizer)
    return db


def _day(s):
    datetime.strptime(s, "%Y-%m-%d")
    return s


def _ts(s):
    for f in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, f).timestamp()
        except ValueError:
            pass
    raise ValueError(f"bad time {s!r}: use YYYY-MM-DD or YYYY-MM-DDTHH:MM")


LINE_SQL = """SELECT s.id, s.ts, s.dur, s.day, s.text, s.media, s.source, COALESCE(sp.name,'Unknown') AS speaker
              FROM segments s LEFT JOIN speakers sp ON sp.id=s.speaker_id"""


def _line(r):
    return {"id": r["id"], "day": r["day"], "time": datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S"), "speaker": r["speaker"],
            "text": r["text"], "media": r["media"] in (1, 2), "source": r["source"] or "room",
            "link": f"{LIVE_URL}/?day={r['day']}#seg-{r['id']}"}


class Tools:
    """Plain methods (testable without MCP); build_server() registers them as MCP tools."""

    def __init__(self, path=None):
        self.path = path

    def db(self):
        return ro_connect(self.path)

    def search(self, query: str, start: str = "", end: str = "", speaker: str = "", limit: int = 30) -> dict:
        """Word search across the transcript (FTS5, stemmed). Optional start/end (YYYY-MM-DD[THH:MM]) and speaker
        name. TV/music lines are excluded. Returns matching lines with links."""
        from .search import fts_query
        d = self.db()
        fq = fts_query(query or "")
        if not fq:
            return {"lines": [], "note": "no searchable words in the query"}
        where, args = ["segments_fts MATCH ?", "s.noise=0", "COALESCE(s.media,0) NOT IN (1,2)"], [fq]
        if start:
            where.append("s.ts>=?"); args.append(_ts(start))
        if end:
            where.append("s.ts<?"); args.append(_ts(end) + (86400 if len(end) == 10 else 0))
        if speaker:
            where.append("LOWER(sp.name) LIKE ?"); args.append("%" + speaker.lower().replace("%", "") + "%")
        n = max(1, min(int(limit), 200))
        rows = d.execute(f"""{LINE_SQL} JOIN segments_fts f ON f.rowid=s.id WHERE {' AND '.join(where)}
                             ORDER BY bm25(segments_fts) LIMIT ?""", args + [n]).fetchall()
        return {"lines": [_line(r) for r in rows]}

    def lines(self, day: str = "", start: str = "", end: str = "", include_media: bool = False, limit: int = 500) -> dict:
        """Transcript lines for a day (YYYY-MM-DD) or a time range, oldest first."""
        d = self.db()
        if day:
            where, args = ["s.day=?"], [_day(day)]
        elif start and end:
            where, args = ["s.ts>=? AND s.ts<?"], [_ts(start), _ts(end)]
        else:
            raise ValueError("give day=YYYY-MM-DD or start and end")
        where.append("s.noise=0")
        if not include_media:
            where.append("COALESCE(s.media,0) NOT IN (1,2)")
        n = max(1, min(int(limit), 5000))
        rows = d.execute(f"{LINE_SQL} WHERE {' AND '.join(where)} ORDER BY s.ts, s.id LIMIT ?", args + [n]).fetchall()
        return {"lines": [_line(r) for r in rows], "truncated": len(rows) == n}

    def speakers(self) -> dict:
        """Known voices with line counts and talk time."""
        d = self.db()
        rows = d.execute("""SELECT sp.name, sp.named, COUNT(s.id) n, COALESCE(SUM(s.dur),0) secs, MAX(s.ts) last FROM speakers sp
                            LEFT JOIN segments s ON s.speaker_id=sp.id AND s.noise=0 WHERE sp.merged_into IS NULL GROUP BY sp.id
                            ORDER BY secs DESC""").fetchall()
        return {"speakers": [{"name": r["name"], "named": bool(r["named"]), "lines": r["n"], "talk_minutes": round(r["secs"] / 60, 1),
                              "last_heard": datetime.fromtimestamp(r["last"]).isoformat(timespec="minutes") if r["last"] else None} for r in rows]}

    def action_items(self, since_day: str = "", unsent_only: bool = False, limit: int = 200) -> dict:
        """Action items found in the transcript (owner, task, due date, source line ids)."""
        d = self.db()
        where, args = [], []
        if since_day:
            where.append("day>=?"); args.append(_day(since_day))
        if unsent_only:
            where.append("sent=0")
        rows = d.execute("SELECT * FROM action_items" + (" WHERE " + " AND ".join(where) if where else "") +
                         " ORDER BY source_ts DESC LIMIT ?", args + [max(1, min(int(limit), 1000))]).fetchall()
        return {"items": [{"id": r["id"], "day": r["day"], "owner": r["owner"], "task": r["task"], "specifics": r["specifics"],
                           "due": r["due"], "sent": bool(r["sent"]), "source_line_ids": __import__("json").loads(r["line_ids"])} for r in rows]}

    def conversations(self, day: str) -> dict:
        """End-of-conversation cards for a day: title, summary, decisions, people."""
        import json
        d = self.db()
        a = datetime.strptime(_day(day), "%Y-%m-%d").timestamp()
        rows = d.execute("SELECT * FROM conversations WHERE ts0>=? AND ts0<? ORDER BY ts0", (a, a + 86400)).fetchall()
        out = []
        for r in rows:
            c = json.loads(r["card"]) if r["card"] and r["card"] != "{}" else {}
            out.append({"start": datetime.fromtimestamp(r["ts0"]).strftime("%H:%M"), "end": datetime.fromtimestamp(r["ts1"]).strftime("%H:%M"),
                        "lines": r["lines"], "title": c.get("title"), "summary": c.get("summary"), "decisions": c.get("decisions", []),
                        "people": json.loads(r["people"] or "[]")})
        return {"conversations": out}

    def daily_summary(self, day: str) -> dict:
        """The nightly recap for a day (headline, decisions, to-dos, who said what), if written."""
        import json
        r = self.db().execute("SELECT body, version, model FROM summaries WHERE day=? ORDER BY version DESC LIMIT 1", (_day(day),)).fetchone()
        return {"day": day, "summary": json.loads(r["body"]) if r else None}

    def ideas(self, status: str = "") -> dict:
        """Ideas captured from speech (title, status, when), newest first. status: new|later|done-it|dismissed."""
        d = self.db()
        if status and status not in ("new", "later", "done-it", "dismissed"):
            raise ValueError("status must be new, later, done-it or dismissed")
        rows = d.execute("SELECT id, title, status, source, created FROM ideas" + (" WHERE status=?" if status else "") +
                         " ORDER BY created DESC LIMIT 200", (status,) if status else ()).fetchall()
        return {"ideas": [{"id": r["id"], "title": r["title"], "status": r["status"], "source": r["source"],
                           "created": datetime.fromtimestamp(r["created"]).isoformat(timespec="minutes")} for r in rows]}


TOOL_NAMES = ["search", "lines", "speakers", "action_items", "conversations", "daily_summary", "ideas"]


def build_server(path=None):
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations
    t = Tools(path)
    srv = MCPServer("ramblr", title="Ramblr (read-only)", instructions=__doc__.split("\n\n")[0])
    ro = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    for name in TOOL_NAMES:
        srv.add_tool(getattr(t, name), name=name, annotations=ro)
    return srv


if __name__ == "__main__":
    build_server().run("stdio")
