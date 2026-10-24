"""Demo mode with fake data (B4-16).

  python -m ramblr.demo build [--out PATH]      make a demo DB: generic people, five weeks of made-up lines, a TV evening,
                                                recaps, action items, conversation cards, an idea with a storyboard,
                                                facts, goals. Deterministic (seeded), no model calls, no room audio.
  python -m ramblr.demo serve [--port 8790]     the full site on 127.0.0.1 over that DB, read-only: every write is refused
                                                (403) and a DEMO banner shows. Never binds 0.0.0.0 and never touches the
                                                live DB, state, spool or sockets.

Making a demo PUBLIC (any non-tailnet exposure) is Fred's call and is not done here."""
import argparse
import hashlib
import json
import os
import random
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

PEOPLE = {"Fred": ["The NPU is the right place for this, the GPU stays free.", "Let me check the logs before we change anything.",
                   "The deploy went fine, all services came back.", "I'll write that up tonight and send it over.",
                   "We should move the backups to the bigger drive.", "That dashboard needs one screen, no scrolling.",
                   "The new build is about thirty percent faster.", "I'm going to rewrite the parser in TypeScript.",
                   "Remind me to renew the domain next week.", "The vendor meeting moved to Thursday."],
          "Sam": ["Did you eat lunch yet?", "The plumber is coming Tuesday at nine.", "Can you pick up dog food on the way back?",
                  "Jordan called, he wants the lake photos.", "I booked the restaurant for Saturday.", "I'll take Biscuit to the vet Friday."],
          "Jordan": ["The game server crashed again last night.", "Send me the photos when you get a chance.", "I'm thinking about a road trip in November."]}
TV = ["And now the weather for Atlanta.", "Highs near seventy tomorrow with a chance of storms.", "Back to you in the studio."]
STORYBOARD = {"title": "Biscuit Watch", "pitch": "A door-side box that tells a stranger bark from a squirrel bark, all on-device.",
              "problem": {"text": "Pet cams stream everything to the cloud and alert on every noise.", "for_whom": "dog owners"},
              "vision": "A small box by the door classifies barks locally and sends one calm, specific alert.",
              "panels": [{"n": i + 1, "caption": c, "scene": s, "image": None} for i, (c, s) in enumerate([
                  ("Box by the door", "A palm-sized board with a mic sits on a shelf."), ("A bark", "Voice activity wakes the classifier."),
                  ("Labelled", "Stranger, squirrel, anxious or play, with a confidence."), ("One alert", "The phone gets a short, specific note."),
                  ("Weekly chart", "A calm chart of anxious minutes while out.")])],
              "features": {"mvp": ["bark classifier on an NPU board", "phone alert"], "later": ["weekly chart"]},
              "build": {"machine": "gus", "who": "Larry", "effort": "a weekend", "stack": "Python + OpenVINO"},
              "risks": ["false alarms"], "open_questions": ["which board"], "quotes": [{"ts": "21:05:00", "text": "A little box by the door that listens for Biscuit barking."}],
              "first_steps": ["record barks", "train a small classifier", "wire a phone alert"], "score": {"value": 7, "verdict": "worth doing", "why": "cheap and useful"},
              "mockups": []}


def build(out):
    os.environ["RAMBLR_DB"] = str(out)
    from . import db as dbm
    if Path(out).exists():
        raise SystemExit(f"{out} exists; pick another --out (nothing is overwritten)")
    rnd = random.Random(7)
    d = dbm.connect(Path(out))
    ids = {n: dbm.new_speaker(d, n, 1) for n in PEOPLE}
    tv = dbm.new_speaker(d)
    d.execute("UPDATE speakers SET confirmed=1 WHERE id=?", (ids["Fred"],))
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    days = [today - timedelta(days=n) for n in range(35, 0, -1)]
    for day in days:
        if rnd.random() < 0.12:
            continue
        for _ in range(rnd.randint(2, 5)):
            t = day.replace(hour=rnd.choice([8, 9, 10, 11, 14, 15, 16, 20])).timestamp() + rnd.randint(0, 2400)
            for _ in range(rnd.randint(4, 16)):
                who = rnd.choices(list(PEOPLE), weights=[6, 3, 1])[0]
                dur = rnd.uniform(1.5, 7)
                sid, _ = dbm.insert_segment(d, t, dur, rnd.choice(PEOPLE[who]), None, "demo", 0)
                d.execute("UPDATE segments SET speaker_id=?, cluster_id=?, diar=1, media=0 WHERE id=?", (ids[who], ids[who], sid))
                t += dur + rnd.uniform(3, 40)
        dd = day.strftime("%Y-%m-%d")
        n = d.execute("SELECT COUNT(*) FROM segments WHERE day=?", (dd,)).fetchone()[0]
        if n:
            d.execute("INSERT INTO summaries(day,version,created,generator,model,lines,body) VALUES(?,1,?,'demo','demo',?,?)",
                      (dd, time.time(), n, json.dumps({"headline": rnd.choice(["Build day: NPU work and a vendor call.", "House logistics and a long debugging session.",
                                                                                 "Planning the week: deploys, vet visit, a road trip idea."]),
                                                       "decisions": ["Keep the nightly job on the cheapest model."], "todos": ["Renew the domain"],
                                                       "who_said": [{"speaker": "Fred", "points": ["Moved backups to the bigger drive"]}], "topics": ["NPU"]})))
    y = days[-1]
    t = y.replace(hour=19).timestamp()
    for i in range(int(3600 / 2.88)):
        d.execute("INSERT INTO media_windows(ts0,ts1,music,tv,crowd,narration,speech,top,rms) VALUES(?,?,0.2,0.75,0,0.3,0.6,518,0.04)", (t + i * 2.88, t + (i + 1) * 2.88))
    for k in range(12):
        sid, _ = dbm.insert_segment(d, t + k * 280, 4, rnd.choice(TV), None, "demo", 0)
        d.execute("UPDATE segments SET speaker_id=?, diar=1, media=1 WHERE id=?", (tv, sid))
    it = y.replace(hour=21, minute=5).timestamp()
    lines = []
    for k, txt in enumerate(["Larry, I have an idea. A little box by the door that listens for Biscuit barking.",
                             "It pings my phone, stranger bark versus squirrel bark.", "Runs on a tiny NPU board, nothing goes to the cloud.", "That's it."]):
        sid, _ = dbm.insert_segment(d, it + k * 9, 6, txt, None, "demo", 0)
        d.execute("UPDATE segments SET speaker_id=?, diar=1, media=0 WHERE id=?", (ids["Fred"], sid))
        lines.append(sid)
    iid = d.execute("INSERT INTO ideas(key,title,status,source,open,created,updated,last_line_ts) VALUES(?,?,'new','trigger',0,?,?,?)",
                    ("trigger:demo", "Biscuit Watch", it, it, it + 40)).lastrowid
    d.executemany("INSERT INTO idea_lines(idea_id,segment_id) VALUES(?,?)", [(iid, s) for s in lines])
    d.execute("INSERT INTO storyboards(idea_id,version,status,created,generator,model,latency_s,body) VALUES(?,1,'ok',?,'demo','demo',0,?)",
              (iid, it + 600, json.dumps(STORYBOARD)))
    for task, owner, due in [("Renew the domain", "Fred", (today + timedelta(days=2)).strftime("%Y-%m-%d")),
                             ("Take Biscuit to the vet", "Sam", (today + timedelta(days=4)).strftime("%Y-%m-%d")),
                             ("Send Jordan the lake photos", "Fred", (today - timedelta(days=1)).strftime("%Y-%m-%d"))]:
        sid = lines[0]
        d.execute("INSERT INTO action_items(key,day,owner,task,specifics,due,due_phrase,line_ids,source_ts,created,model) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (hashlib.sha1(task.encode()).hexdigest()[:20], y.strftime("%Y-%m-%d"), owner, task, "", due, "", json.dumps([sid]), it, time.time(), "demo"))
    d.execute("""INSERT INTO conversations(key, ts0, ts1, first_seg, last_seg, lines, status, people, card, model, created)
                 VALUES('conv:demo', ?, ?, ?, ?, 4, 'closed', ?, ?, 'demo', ?)""",
              (it, it + 40, lines[0], lines[-1], json.dumps(["Fred"]), json.dumps({"title": "Bark box idea", "summary": "Fred pitched a door-side bark classifier on an NPU board.",
                                                                                  "context": "solo ramble", "sections": {"ideas": ["bark box"], "todos": [], "threads": ["Biscuit"]},
                                                                                  "decisions": [], "action_items": []}), time.time()))
    d.execute("INSERT INTO facts(key,subject,fact,kind,first_seen,last_seen,mentions,line_ids,status,created) VALUES('f1','Fred','Fred prefers TypeScript over Python.','preference',?,?,3,'[]','active',?)",
              (y.strftime("%Y-%m-%d"), y.strftime("%Y-%m-%d"), time.time()))
    d.execute("INSERT INTO goals(key,owner,goal,by_phrase,first_seen,last_seen,mentions,line_ids,status,created) VALUES('g1','Fred','Ship the bark box prototype','next month',?,?,1,'[]','open',?)",
              (y.strftime("%Y-%m-%d"), y.strftime("%Y-%m-%d"), time.time()))
    d.execute("INSERT INTO meta(key,value) VALUES('demo','1')")
    return {"db": str(out), "lines": d.execute("SELECT COUNT(*) FROM segments").fetchone()[0], "days": len(days)}


def serve(db_path, port):
    tmp = Path(tempfile.mkdtemp(prefix="ramblr-demo-"))
    os.environ.update({"RAMBLR_DB": str(db_path), "RAMBLR_DATA": str(tmp), "RAMBLR_STATE": str(tmp / "state.json"),
                       "RAMBLR_SPOOL": str(tmp / "spool"), "RAMBLR_SOCK": str(tmp / "n.sock"), "RAMBLR_IDEAS_SOCK": str(tmp / "i.sock"),
                       "RAMBLR_AUDIO24_DIR": str(tmp / "a24"), "RAMBLR_FAMILY_DB": str(tmp / "family.db"), "RAMBLR_PORT": str(port),
                       "RAMBLR_DEMO": "1", "RAMBLR_CONFIG": str(tmp / "cfg")})
    if Path(db_path).resolve() == (Path.home() / ".local/share/ramblr/ears.db").resolve():
        raise SystemExit("refusing to serve the live DB as a demo")
    from . import web
    import uvicorn
    print(f"demo on http://127.0.0.1:{port} (read-only, localhost only)")
    uvicorn.run(web.app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "serve"])
    ap.add_argument("--out", default=str(Path.home() / ".local/share/ramblr/demo/demo.db"))
    ap.add_argument("--db", default=str(Path.home() / ".local/share/ramblr/demo/demo.db"))
    ap.add_argument("--port", type=int, default=8790)
    a = ap.parse_args()
    if a.cmd == "build":
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        print(json.dumps(build(a.out)))
    else:
        serve(a.db, a.port)
