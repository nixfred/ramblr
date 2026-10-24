"""Ramblr voice command router (B3-14 target; Fred, 2026-10-24 7:01 AM: "Have Larry build a tailnet-only voice
router on gus and generate its key").

  ramblr-ideas (commands.route) --POST /route, Bearer RAMBLR_VOICE_ROUTER_KEY--> this router (ramblr-router.service)

- Binds 127.0.0.1 and gus's tailnet IP only (two listeners, port RAMBLR_ROUTER_PORT, default 8783). Never 0.0.0.0,
  never funnel, no tailscale serve entry. Plain HTTP inside the tailnet (WireGuard).
- Every endpoint needs the bearer key; anything else is 401. The key lives only in secrets.env, never printed.
- Default-deny: a task that could send, post, spend, delete, deploy or change anything is HELD as "needs Fred's
  yes" and never runs on its own; Fred approves it on the Commands page (POST /approve/<command_id>). Only tasks
  that read clearly as read-only asks (what/who/check/look up/summarize...) run right away.
- "Run" = forward to Fred's bots through the optional upstream RAMBLR_GROKBOT_WEBHOOK_URL + _KEY (Larry Manager
  "Ramblr voice command router" routine panel, added with `addsecret`). Until those exist a read-only ask is
  logged as "ready" and an approved one as "approved"; nothing else happens.
- Every request is logged in its own DB (router.db, rows never deleted); the result goes back to Ramblr, which
  shows it on the Commands page."""
import hmac
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request

from fastapi import FastAPI, HTTPException, Request

from . import config

log = logging.getLogger("ramblr.router")
RISKY = re.compile(r"\b(send|sends|sending|e-?mail|text|message|dm|post|posting|tweet|publish|reply|respond|forward|share|upload|"
                   r"buy|order|purchase|pay|spend|charge|subscribe|book|donate|transfer|wire|invoice|"
                   r"delete|remove|erase|wipe|drop|purge|destroy|kill|shut ?down|reboot|reset|uninstall|"
                   r"deploy|push|merge|commit|release|ship|install|upgrade|update|change|edit|write|rename|move|"
                   r"cancel|sign|accept|decline|approve|invite|call|schedule|create|make|build|run|start|stop|continue|fix)\b", re.I)
READ = re.compile(r"^(?:please\s+)?(?:what|what's|who|who's|when|where|why|how|which|is|are|was|were|does|do|did|can you tell|"
                  r"check|look up|look into|look at|find|search|summari[sz]e|show|list|explain|research|read|tell me|"
                  r"give me|remind me what|get (?:me )?the status|status of|report)\b", re.I)
SCHEMA = """
CREATE TABLE IF NOT EXISTS router_log(
  id INTEGER PRIMARY KEY, command_id TEXT NOT NULL UNIQUE, received REAL NOT NULL, target TEXT, task TEXT,
  transcript TEXT, speaker TEXT, confidence REAL, cls TEXT NOT NULL, why TEXT, status TEXT NOT NULL, result TEXT,
  approved_at REAL, forwarded_at REAL);
CREATE TRIGGER IF NOT EXISTS router_log_never_delete BEFORE DELETE ON router_log
BEGIN SELECT RAISE(ABORT, 'router log rows are never deleted'); END;
"""


def classify(task):
    """('read', '') only for a clear read-only ask with no risky word; otherwise ('action', why)."""
    t = " ".join((task or "").split())
    m = RISKY.search(t)
    if m:
        return "action", f"'{m.group(0).lower()}' could change something"
    if READ.match(t):
        return "read", ""
    return "action", "not clearly a read-only ask"


def key():
    return os.environ.get("RAMBLR_VOICE_ROUTER_KEY", "")


def upstream():
    return os.environ.get("RAMBLR_GROKBOT_WEBHOOK_URL", ""), os.environ.get("RAMBLR_GROKBOT_WEBHOOK_KEY", "")


def connect(path=None):
    d = sqlite3.connect(str(path or config.ROUTER_DB), isolation_level=None, check_same_thread=False, timeout=10)
    d.row_factory = sqlite3.Row
    d.execute("PRAGMA journal_mode=WAL")
    d.executescript(SCHEMA)
    try:
        os.chmod(path or config.ROUTER_DB, 0o600)
    except OSError:
        pass
    return d


def forward(row, approved):
    """POST to the Grok bot upstream if configured. Returns (status, result); never raises, never logs the URL/key."""
    url, k = upstream()
    if not (url and k):
        return ("approved" if approved else "ready"), "upstream not configured yet (RAMBLR_GROKBOT_WEBHOOK_URL/KEY): logged only"
    body = {k2: row[k2] for k2 in ("command_id", "target", "task", "transcript", "speaker", "confidence")}
    body.update(cls=row["cls"], approved_by_fred=bool(approved), source="ramblr-voice-router")
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {k}", "Idempotency-Key": row["command_id"]})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return ("forwarded" if 200 <= r.status < 300 else "upstream-error"), f"upstream http {r.status}"
    except Exception as e:
        return "upstream-error", f"upstream {type(e).__name__}"


def make_app(db_path=None):
    app = FastAPI(title="Ramblr voice router", docs_url=None, redoc_url=None, openapi_url=None)
    db = connect(db_path)

    def auth(request):
        got = request.headers.get("authorization", "")
        got = got[7:].strip() if got.lower().startswith("bearer ") else ""
        if not key() or not hmac.compare_digest(got, key()):
            raise HTTPException(401, "router key required")

    def view(r):
        return {"command_id": r["command_id"], "class": r["cls"], "status": r["status"], "why": r["why"] or "", "result": r["result"] or ""}

    @app.post("/route")
    async def route(request: Request):
        auth(request)
        p = await request.json()
        cid = str(p.get("command_id") or "")[:64]
        if not cid or not p.get("task"):
            raise HTTPException(400, "command_id and task required")
        old = db.execute("SELECT * FROM router_log WHERE command_id=?", (cid,)).fetchone()
        if old:
            return view(old)                               # idempotent: the same command is decided once
        cls, why = classify(str(p["task"]))
        db.execute("""INSERT INTO router_log(command_id, received, target, task, transcript, speaker, confidence, cls, why, status, result)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                   (cid, time.time(), str(p.get("target", ""))[:80], str(p["task"])[:500], str(p.get("transcript", ""))[:1000],
                    p.get("speaker"), p.get("speaker_confidence"), cls, why,
                    "held" if cls == "action" else "received", "needs Fred's yes" if cls == "action" else ""))
        row = db.execute("SELECT * FROM router_log WHERE command_id=?", (cid,)).fetchone()
        if cls == "action" and p.get("fred_approved") is True:
            # Fred already pressed Approve in Ramblr (the voice gate held it there): one yes is enough
            db.execute("UPDATE router_log SET approved_at=? WHERE id=?", (time.time(), row["id"]))
            st, res = forward(row, approved=True)
            db.execute("UPDATE router_log SET status=?, result=?, forwarded_at=? WHERE id=?",
                       (st, "Fred said yes in Ramblr; " + res, time.time() if st == "forwarded" else None, row["id"]))
            row = db.execute("SELECT * FROM router_log WHERE id=?", (row["id"],)).fetchone()
        elif cls == "read":                                # read-only asks run right away
            st, res = forward(row, approved=False)
            db.execute("UPDATE router_log SET status=?, result=?, forwarded_at=? WHERE id=?",
                       (st, res, time.time() if st == "forwarded" else None, row["id"]))
            row = db.execute("SELECT * FROM router_log WHERE id=?", (row["id"],)).fetchone()
        log.info("command %s: %s -> %s", cid[:12], cls, row["status"])
        return view(row)

    @app.post("/approve/{cid}")
    def approve(cid: str, request: Request):
        auth(request)
        row = db.execute("SELECT * FROM router_log WHERE command_id=?", (cid,)).fetchone()
        if not row:
            raise HTTPException(404, "unknown command")
        if row["status"] != "held":
            return view(row)
        db.execute("UPDATE router_log SET approved_at=? WHERE id=?", (time.time(), row["id"]))
        st, res = forward(row, approved=True)
        db.execute("UPDATE router_log SET status=?, result=?, forwarded_at=? WHERE id=?",
                   (st, "Fred said yes; " + res, time.time() if st == "forwarded" else None, row["id"]))
        log.info("command %s approved by Fred -> %s", cid[:12], st)
        return view(db.execute("SELECT * FROM router_log WHERE id=?", (row["id"],)).fetchone())

    @app.get("/log")
    def recent(request: Request, limit: int = 50):
        auth(request)
        u, k = upstream()
        return {"upstream": "configured" if u and k else "stub",
                "commands": [view(r) for r in db.execute("SELECT * FROM router_log ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))]}

    return app


def hosts():
    """127.0.0.1 plus gus's tailnet IPv4. Never a wildcard address."""
    ip = os.environ.get("RAMBLR_ROUTER_TAILNET_IP") or subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True,
                                                                         timeout=10).stdout.split()[0:1]
    ip = ip if isinstance(ip, str) else (ip[0] if ip else "")
    if not re.fullmatch(r"100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}", ip):
        raise SystemExit(f"no tailnet IPv4 (got {ip!r}); refusing to bind anything else")
    return ["127.0.0.1", ip]


def main():
    import asyncio
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stdout)
    if not key():
        raise SystemExit("RAMBLR_VOICE_ROUTER_KEY missing from secrets.env")
    app = make_app()
    hs = hosts()
    servers = [uvicorn.Server(uvicorn.Config(app, host=h, port=config.ROUTER_PORT, log_level="warning")) for h in hs]
    log.warning("voice router on %s port %d (upstream %s)", " + ".join(hs), config.ROUTER_PORT,
                "configured" if all(upstream()) else "stub")

    async def run():
        await asyncio.gather(*(s.serve() for s in servers))
    asyncio.run(run())


if __name__ == "__main__":
    main()
