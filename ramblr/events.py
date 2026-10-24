"""Event outbox + outgoing webhooks (B3-12).

Code that makes something new calls emit(db, kind, payload): idea.new, idea.storyboard, action_item.new,
conversation.card, upload.done. Every event is kept (never deleted). Webhooks are OFF until Fred creates
~/.config/ramblr/webhooks.json:

  [{"url": "https://larry-manager.example/hook", "events": ["conversation.card", "idea.storyboard"], "secret_env": "RAMBLR_HOOK_KEY"}]

Each delivery is a JSON POST signed with HMAC-SHA256 over the body (header X-Ramblr-Signature: sha256=<hex>,
key from the named variable in secrets.env, never printed or logged) plus X-Ramblr-Event and an Idempotency-Key.
Bounded retries (3 tries, backing off); failures are recorded, never looped forever. Nothing is posted as Fred:
the payload is Ramblr's own data, to an endpoint Fred chose."""
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.request

from . import config

log = logging.getLogger("ramblr.events")
KINDS = ("idea.new", "idea.storyboard", "action_item.new", "conversation.card", "upload.done")
MAX_TRIES = 3


def emit(db, kind, payload):
    if kind not in KINDS:
        raise ValueError(f"unknown event {kind}")
    return db.execute("INSERT INTO events(kind, payload, created) VALUES(?,?,?)", (kind, json.dumps(payload, default=str), time.time())).lastrowid


def hooks():
    p = config.CONFIG_DIR / "webhooks.json"
    if not p.exists():
        return []
    try:
        hs = json.loads(p.read_text())
        return [h for h in hs if isinstance(h, dict) and str(h.get("url", "")).startswith(("https://", "http://127.0.0.1", "http://localhost"))]
    except (OSError, ValueError) as e:
        log.warning("webhooks.json ignored: %s", e)
        return []


def _post(h, ev, body):
    key = os.environ.get(h.get("secret_env") or "", "")
    headers = {"Content-Type": "application/json", "X-Ramblr-Event": ev["kind"], "Idempotency-Key": f"ramblr-event-{ev['id']}"}
    if key:
        headers["X-Ramblr-Signature"] = "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(h["url"], data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def dispatch(db, now=None):
    """Deliver pending events to matching hooks (called from the ideas worker every pass)."""
    now = now or time.time()
    hs = hooks()
    if not hs:
        return {"hooks": 0}
    sent = failed = 0
    for ev in db.execute("""SELECT * FROM events WHERE id > COALESCE((SELECT CAST(value AS INTEGER) FROM meta WHERE key='events_seen'), 0)
                            ORDER BY id LIMIT 100""").fetchall():
        for i, h in enumerate(hs):
            if h.get("events") and ev["kind"] not in h["events"]:
                continue
            db.execute("INSERT OR IGNORE INTO deliveries(event_id, hook, status, tries, created) VALUES(?,?, 'pending', 0, ?)", (ev["id"], i, now))
        db.execute("INSERT INTO meta(key,value) VALUES('events_seen',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (ev["id"],))
    for d in db.execute("SELECT d.*, e.kind, e.payload, e.created AS ev_created FROM deliveries d JOIN events e ON e.id=d.event_id "
                        "WHERE d.status='pending' AND d.next_try<=? ORDER BY d.event_id LIMIT 50", (now,)).fetchall():
        if d["hook"] >= len(hs):
            db.execute("UPDATE deliveries SET status='dropped', error='hook removed from webhooks.json' WHERE event_id=? AND hook=?", (d["event_id"], d["hook"]))
            continue
        ev = {"id": d["event_id"], "kind": d["kind"]}
        body = json.dumps({"event": d["kind"], "id": d["event_id"], "at": d["ev_created"], "data": json.loads(d["payload"]),
                           "source": "ramblr"}).encode()
        try:
            st = _post(hs[d["hook"]], ev, body)
            ok = 200 <= st < 300
            err = None if ok else f"http {st}"
        except Exception as e:
            ok, err = False, type(e).__name__     # never the URL or key
        tries = d["tries"] + 1
        status = "sent" if ok else ("failed" if tries >= MAX_TRIES else "pending")
        db.execute("UPDATE deliveries SET status=?, tries=?, error=?, next_try=?, sent_at=? WHERE event_id=? AND hook=?",
                   (status, tries, err, now + 60 * (4 ** tries), now if ok else None, d["event_id"], d["hook"]))
        sent += ok
        failed += status == "failed"
    return {"hooks": len(hs), "sent": sent, "failed": failed}
