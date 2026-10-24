"""F (Fred, 2026-10-24): vic seg 10 "Have the finance bot look up all my subscriptions and write a report" never
routed (the wake word was clipped). Natural phrasing + bot aliases, mic:* sources, Fred's voice gate on mic:vic,
unidentified voices fail CLOSED (held for Fred's yes, not dropped). Temp DBs; upstream stays stubbed."""
import importlib
import socket
import threading
import time

import pytest

SEG10 = "Have the finance bot look up all my subscriptions and write a report"


def test_phrasing_and_aliases():
    from ramblr import commands as c
    importlib.reload(c)
    cases = {
        SEG10: ("Finances", "look up all my subscriptions and write a report"),
        "Larry, tell Grok Bot to summarize the deck notes.": ("Grok Bot", "summarize the deck notes."),
        "Tell Larry to have the finance bot pull my October statements": ("Finances", "pull my October statements"),
        "Larry, ask the research bot to find flights to Denver": ("research bot", "find flights to Denver"),
        "random bot to continue working on Ramblr.": ("random bot", "continue working on Ramblr."),
        "larry tell vic to rebuild the plugin": ("vic", "rebuild the plugin"),
    }
    for text, want in cases.items():
        assert c.parse(text) == want, text
    for text in ("I told Larry about it", "have the kids clean up their room", "Larry, have a good day",
                 "tell them to stop it", "We should ask the bank to waive the fee", "Get the grocery list ready"):
        assert c.parse(text) is None, text


@pytest.fixture
def rt(tmp_path, monkeypatch):
    import uvicorn
    monkeypatch.setenv("RAMBLR_VOICE_ROUTER_KEY", "k")
    monkeypatch.delenv("RAMBLR_GROKBOT_WEBHOOK_URL", raising=False)
    from ramblr import config, db, commands, me, router
    for m in (db, me, commands, router):
        importlib.reload(m)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(router.make_app(tmp_path / "r.db"), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    while not srv.started:
        time.sleep(0.05)
    monkeypatch.setenv("RAMBLR_VOICE_ROUTER_URL", f"http://127.0.0.1:{port}/route")
    d = db.connect(config.DB_PATH)
    fred = db.new_speaker(d, "Fred Nix", 1)
    d.execute("UPDATE speakers SET confirmed=1 WHERE id=?", (fred,))
    jordan = db.new_speaker(d, "Jordan Lee", 1)
    stranger = db.new_speaker(d)                               # an unnamed voice
    yield {"d": d, "db": db, "c": commands, "fred": fred, "jordan": jordan, "stranger": stranger}
    srv.should_exit = True


def say(env, ts, text, spk):
    sid, _ = env["db"].insert_segment(env["d"], ts, 5.0, text, None, "t", 1, source="mic:vic")
    env["d"].execute("UPDATE segments SET media=0, diar=1, speaker_id=?, sim=0.9 WHERE id=?", (spk, sid))
    return sid


def test_seg10_replay_on_mic_vic_routes_and_unknown_voice_is_held(rt):
    d, c = rt["d"], rt["c"]
    t = 1792760000
    a = say(rt, t, SEG10, rt["fred"])
    b = say(rt, t + 30, "Larry, tell the finance bot to delete my old budget", rt["stranger"])
    x = say(rt, t + 60, "Larry, have the finance bot cancel Netflix", rt["jordan"])
    c.detect(d, now=t + 200)
    c.route(d, now=t + 300)
    got = {r["segment_id"]: dict(r) for r in d.execute("SELECT * FROM command_intents")}
    assert got[a]["target"] == "Finances" and got[a]["status"] == "held"          # router: "write a report" is an action
    assert "needs Fred's yes" in got[a]["result"] and got[a]["voice_hold"] == 0
    assert got[b]["status"] == "held" and got[b]["voice_hold"] == 1 and got[b]["sent_at"] is None   # never left Ramblr
    assert got[x]["status"] == "ignored"                                            # Jordan said it, not Fred
    assert c.approve(d, got[b]["command_id"]) == "queued"                          # one yes ...
    c.route(d, now=t + 400)
    r = d.execute("SELECT status, result FROM command_intents WHERE segment_id=?", (b,)).fetchone()
    assert r["status"] == "approved" and "Fred said yes" in r["result"]             # ... carried through the router


# --- one spoken command, several lines (Fred 11:50: segments 250-253 never routed) -------------------------------

def line(env, ts, dur, text, spk, source=None):
    sid, _ = env["db"].insert_segment(env["d"], ts, dur, text, None, "t", 1, source=source)
    env["d"].execute("UPDATE segments SET media=0, diar=1, speaker_id=?, sim=0.9 WHERE id=?", (spk, sid))
    return sid


SUBTRAKR = [(0.0, 3.2, "Hey Larry, have Grockbot"), (3.8, 4.5, "finance, go look at B Scott, the GitHub user."),
            (8.9, 7.1, "and install a program called SubTracker, S-U-B-T-R-A-C-R."),
            (19.9, 4.2, "I want to use it to track my subscriptions."), (24.7, 1.7, "Set it up on gas.")]


def speak(env, t, lines, spk):
    return [line(env, t + dt, dur, text, spk) for dt, dur, text in lines]


def test_multi_line_command_is_joined_held_by_router(rt):
    d, c = rt["d"], rt["c"]
    t = 1792760000
    ids = speak(rt, t, SUBTRAKR, rt["fred"])
    assert c.detect(d, now=t + 30) == []                     # the last line is not settled yet: wait, consume nothing
    c.detect(d, now=t + 45)
    c.route(d, now=t + 50)
    rows = d.execute("SELECT * FROM command_intents").fetchall()
    assert len(rows) == 1, [dict(r) for r in rows]
    r = rows[0]
    assert r["segment_id"] == ids[0] and r["target"] == "Grok Bot"
    assert "install a program" in r["task"] and r["task"].endswith("Set it up on gus.")      # fuzzy host
    assert r["phrase"].startswith("Hey Larry, have Grockbot finance, go look at")
    assert r["status"] == "held" and "install" in r["result"]                          # default-deny: never runs alone


def test_missing_wake_word_still_routes_when_a_bot_is_named(rt):
    d, c = rt["d"], rt["c"]
    t = 1792760000
    speak(rt, t, [(0.0, 3.2, "have Grockbot")] + SUBTRAKR[1:], rt["fred"])
    c.detect(d, now=t + 45)
    r = d.execute("SELECT * FROM command_intents").fetchone()
    assert r and r["target"] == "Grok Bot" and "track my subscriptions" in r["task"]


def test_live_250_253_without_wake_or_bot_does_not_route(rt):
    """What the room mic actually stored at 11:50: nobody is addressed, so nothing routes (fail closed)."""
    d, c = rt["d"], rt["c"]
    t = 1792760000
    speak(rt, t, [(0.0, 2.7, "get B Scott, the GitHub user."), (4.0, 6.4, "install a program called Subtrakr, S-U-B-T-R-A-C-R."),
                  (14.6, 3.9, "I want to use it to track my subscriptions."), (18.9, 1.8, "Set it up on gus.")], rt["fred"])
    c.detect(d, now=t + 45)
    assert d.execute("SELECT COUNT(*) FROM command_intents").fetchone()[0] == 0


def test_another_voice_ends_the_command(rt):
    d, c = rt["d"], rt["c"]
    t = 1792760000
    line(rt, t, 3.0, "Larry, have the finance bot", rt["fred"])
    line(rt, t + 3.5, 2.0, "check my balance", rt["fred"])
    line(rt, t + 6.0, 2.0, "and delete everything", rt["jordan"])
    c.detect(d, now=t + 30)
    r = d.execute("SELECT * FROM command_intents").fetchone()
    assert r["target"] == "Finances" and r["task"] == "check my balance"


def test_long_gap_is_not_joined(rt):
    d, c = rt["d"], rt["c"]
    t = 1792760000
    line(rt, t, 3.0, "Larry, have the finance bot check my balance", rt["fred"])
    line(rt, t + 3.0 + 12, 2.0, "and also delete my account", rt["fred"])
    c.detect(d, now=t + 40)
    r = d.execute("SELECT * FROM command_intents").fetchone()
    assert r["task"] == "check my balance"


def test_hosts_and_chatter():
    from ramblr import commands as c
    assert c.hosts("Set it up on gas.") == "Set it up on gus."
    assert c.hosts("we are out of gas") == "we are out of gas"
    assert c.parse("we are out of gas, the robot vacuum is stuck") is None
    assert c.parse("Robot, clean the kitchen") is None
    assert c.parse("Grockbot, go look at B Scott") == ("Grok Bot", "go look at B Scott")
