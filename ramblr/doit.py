"""DO IT webhook sender (used by phase 2 ideas). Reads URL and key from config (secrets.env via systemd
EnvironmentFile). Unset -> 'pending webhook'. Idempotent on idea_id. Never logs the URL or the key."""
import json
import time
import urllib.request

from . import config, db as dbm


def status():
    return "ready" if config.DOIT_WEBHOOK_URL and config.DOIT_WEBHOOK_KEY else "pending webhook"


def payload(idea_id, gist, brief, raw_lines, storyboard=None):
    p = {"idea_id": idea_id, "gist": gist, "brief": brief,
         "raw_lines": [{"ts": l["ts"], "speaker": l["speaker"], "text": l["text"]} for l in raw_lines],
         "source": "ramblr", "sent_at": time.time()}
    if storyboard is not None:       # new OPTIONAL field; every existing field is unchanged
        p["storyboard"] = storyboard
    return p


def send(db, idea_id, gist, brief, raw_lines, storyboard=None, timeout=15):
    """Returns 'sent', 'already sent', 'pending webhook' or 'error <http status/class>'."""
    if status() != "ready":
        return "pending webhook"
    if dbm.get_meta(db, f"doit_sent:{idea_id}"):
        return "already sent"
    body = json.dumps(payload(idea_id, gist, brief, raw_lines, storyboard)).encode()
    req = urllib.request.Request(config.DOIT_WEBHOOK_URL, data=body, method="POST", headers={
        "Content-Type": "application/json", "Idempotency-Key": str(idea_id),
        config.DOIT_KEY_HEADER: config.DOIT_KEY_FORMAT.format(key=config.DOIT_WEBHOOK_KEY)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ok = 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        return f"error {e.code}"
    except Exception as e:
        return f"error {type(e).__name__}"
    if ok:
        dbm.set_meta(db, f"doit_sent:{idea_id}", time.time())
        return "sent"
    return "error status"
