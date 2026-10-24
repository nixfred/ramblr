"""Ideas service: spots spoken ideas, and builds storyboards on request or after a quiet period.
Event driven: the web process sends a unix datagram for 'Build idea now'; otherwise it wakes every 30 s."""
import json
import logging
import os
import socket
import sys
import time

from . import ambient, calendar_feed, commands, config, conversations, db as dbm, events, imports, ideas, musicid, notify, panelart, players, plugins, storyboard, uploads

log = logging.getLogger("ramblr.ideas")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stdout)
    db = dbm.connect(config.DB_PATH)
    path = str(config.IDEAS_SOCK)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(path)
    os.chmod(path, 0o600)
    sock.settimeout(30)
    # on start every 'building' row is an orphan of a previous process
    n = db.execute("UPDATE storyboards SET status='error', error='interrupted (worker restarted mid-build)' WHERE status='building'").rowcount
    if n:
        log.warning("marked %d interrupted storyboard build(s) as failed", n)
    log.warning("ideas worker ready: quiet=%ss gap=%ss generator=%s", config.IDEA_QUIET, config.IDEA_GAP, config.SB_GEN)
    last_poll = 0.0
    tr = uploads.Transcriber()
    while True:
        try:
            sock.recv(4096)
        except socket.timeout:
            pass
        try:
            if time.monotonic() - last_poll >= 30:       # B2-2 media log: player sources + pending music lookups
                last_poll = time.monotonic()
                players.poll_all(db)
                musicid.lookup_pending(db)
        except Exception:
            log.exception("media log poll failed, continuing")
        try:
            for uid in uploads.pending(db):                 # B3-0: iPhone rambles through the same NPU pipeline
                log.info("transcribing upload %d", uid)
                uploads.process(db, uid, tr)
        except Exception:
            log.exception("upload pass failed, continuing")
        try:
            c = conversations.update(db)                    # B3-7: conversations, rolling live summary, cards
            if c["closed"] or c["live"]:
                log.info("conversations: %s", c)
        except Exception:
            log.exception("conversation pass failed, continuing")
        try:
            calendar_feed.refresh(db)                          # B4-6 (every 15 min, stub without the iCal address)
            ambient.snapshot(db)                               # B4-13 (every 5 min)
        except Exception:
            log.exception("calendar/ambient pass failed, continuing")
        try:
            for r in imports.scan_inbox(db):                  # B4-1: Zoom / Wispr files dropped into the inbox
                log.info("import %s", r)
        except Exception:
            log.exception("import inbox failed, continuing")
        try:
            commands.detect(db)                               # B3-14: "Larry, tell <bot> to <task>"
            commands.route(db)
        except Exception:
            log.exception("voice routing pass failed, continuing")
        try:
            ran = plugins.tick(db)                             # B3-12: Fred's scheduled prompt files
            if ran:
                log.info("plugins ran: %s", ran)
            events.dispatch(db)                                # B3-12: webhooks (only if webhooks.json exists)
        except Exception:
            log.exception("plugins/webhooks pass failed, continuing")
        try:
            storyboard.recover_stale(db)
            for iid in ideas.scan(db):
                notify.send({"t": "idea", "id": iid})
            for iid in ideas.due(db):
                row = db.execute("SELECT build_requested FROM ideas WHERE id=?", (iid,)).fetchone()
                log.info("building storyboard for idea %d", iid)
                sid = storyboard.build(db, iid, rebuild=row["build_requested"] == 2)
                r = db.execute("SELECT status, generator, latency_s, error FROM storyboards WHERE id=?", (sid,)).fetchone() if sid else None
                if r:
                    log.info("idea %d storyboard %s via %s in %ss %s", iid, r["status"], r["generator"], r["latency_s"], r["error"] or "")
        except Exception:
            log.exception("ideas pass failed, continuing")
        try:
            art = panelart.fill(db)                            # B4-18: pencil sketches for empty panel slots (NPU, CPU nice 19)
            if art:
                log.info("panel art: %s", art)
        except Exception:
            log.exception("panel art pass failed, continuing")


if __name__ == "__main__":
    main()
