"""Call mode and Fred's carried "not TV" (2026-10-24 Zoom call: a caller out of the speakers was tagged TV
p 0.89-0.93 again after every "not TV" click), plus the relabel picker's repeat saves. Temp DB only (conftest)."""
import importlib
import time
from datetime import datetime

import numpy as np
import pytest

T = datetime(2026, 10, 24, 12, 30).timestamp()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAMBLR_DB", str(tmp_path / "t.db"))
    from ramblr import config
    importlib.reload(config)
    from ramblr import db, media, calls, diarize, speakers, state
    for m in (db, state, calls, media, diarize, speakers):
        importlib.reload(m)
    return {"db": db.connect(), "dbm": db, "media": media, "calls": calls, "diarize": diarize, "spk": speakers, "state": state}


def tvwindows(env, t0, t1, tv=0.9, source=None):
    t = t0
    while t < t1:
        env["db"].execute("INSERT INTO media_windows(ts0,ts1,music,tv,crowd,narration,speech,top,rms,source) VALUES(?,?,0,?,0,0,0.6,0,0.05,?)",
                          (t, t + 2.88, tv, source))
        t += 2.88


def line(env, t, dur, speaker, source=None, text="and then we moved the whole shop to Atlanta"):
    sid, _ = env["dbm"].insert_segment(env["db"], t, dur, text, None, "t", 1, source=source)
    env["db"].execute("UPDATE segments SET speaker_id=?, cluster_id=?, diar=1 WHERE id=?", (speaker, speaker, sid))
    return sid


def media_of(env, sid):
    return env["db"].execute("SELECT media FROM segments WHERE id=?", (sid,)).fetchone()[0]


def caller(env):
    return env["dbm"].new_speaker(env["db"])           # the caller's voice: not a named person yet


# --- detection ---------------------------------------------------------------------------------------------------

def so(name, binary=None, media="", corked=False):
    return {"corked": corked, "properties": {"application.name": name, "application.process.binary": binary, "media.name": media}}


def test_detect(env):
    c = env["calls"]
    assert c.detect([so("ZOOM VoiceEngine", "zoom")], []) == "zoom"
    assert c.detect([so("Brave", "brave", "WebRTC")], []) == "brave"                       # Meet / Zoom web in a browser
    assert c.detect([so("ramblr", "pacat", "ramblr room transcription"), so("Quickshell Peak Detect", None, "Peak detect"),
                     so(None, None, "cava")], [so(None, None, "Govee Lamps Stereo Pair output")]) is None   # gus at 12:4x today
    assert c.detect([so("Brave", "brave", corked=True)], []) is None                         # a paused capture is no call
    assert c.detect([], [so("Microsoft Teams", "teams")]) == "teams"
    assert c.detect([], [so("Brave", "brave", "YouTube")]) is None                           # a browser playing video is not a call


def test_record_extends_and_during(env):
    c, d = env["calls"], env["db"]
    a = c.record(d, T, T + 5, "zoom", "auto")
    assert c.record(d, T + 20, T + 25, "zoom", "auto") == a                                  # seen again within 30 s: same stretch
    b = c.record(d, T + 200, T + 205, "zoom", "auto")
    assert b != a
    assert c.during(d, T + 10, T + 12) and c.during(d, T + 201, T + 202) and not c.during(d, T + 100, T + 150)


def test_call_switch_expires(env, monkeypatch):
    from ramblr import ctl
    importlib.reload(ctl)
    monkeypatch.setattr(ctl, "toast", lambda *_: None)
    assert ctl.main(["call", "on", "1"]) == 0
    on, until = env["calls"].switch()
    assert on and until > time.time()
    assert not env["calls"].switch(time.time() + 120)[0]                                     # switches itself off
    ctl.main(["call", "off"])
    assert not env["calls"].switch()[0]


# --- tagging -----------------------------------------------------------------------------------------------------

def test_caller_is_tv_without_call_mode_and_talk_with_it(env):
    d, m = env["db"], env["media"]
    w = caller(env)
    tvwindows(env, T, T + 400)
    ids = [line(env, T + i * 20, 18, w) for i in range(15)]
    m.tag_pending(d, until=T + 500)
    assert sum(media_of(env, i) == 1 for i in ids) >= 10                                     # the bug: the caller is "TV"
    d.execute("UPDATE segments SET media=NULL, media_p=NULL")
    env["calls"].record(d, T - 5, T + 400, "call switch", "manual")
    m.tag_pending(d, until=T + 500)
    assert all(media_of(env, i) == 0 for i in ids)                                           # call mode: never auto media


def test_fred_not_tv_carries_forward_until_a_pause_or_a_tv_mark(env):
    d, m = env["db"], env["media"]
    w = caller(env)
    tvwindows(env, T, T + 1000)
    first = [line(env, T + i * 20, 18, w) for i in range(3)]
    m.tag_pending(d, until=T + 70)
    assert media_of(env, first[0]) == 1
    m.correct(d, first, False)                                                               # Fred: "not TV" (-1)
    later = [line(env, T + 60 + i * 20, 18, w) for i in range(5)]                            # the next lines of the call
    m.tag_pending(d, until=T + 200)
    assert all(media_of(env, i) == 0 for i in later)                                         # carried: no auto TV
    after_pause = line(env, T + 60 + 5 * 20 + 18 + 90, 18, w)                                # 90 s of silence ends the stretch
    m.tag_pending(d, until=T + 400)
    assert media_of(env, after_pause) == 1
    m.correct(d, [after_pause], True)                                                        # Fred: "that was the TV" (2)
    nxt = line(env, T + 400, 18, w)
    m.tag_pending(d, until=T + 500)
    assert media_of(env, nxt) == 1                                                           # the newer label wins


def test_retag_honours_the_carry(env):
    d, m = env["db"], env["media"]
    w = caller(env)
    tvwindows(env, T, T + 200)
    ids = [line(env, T + i * 20, 18, w) for i in range(6)]
    m.tag_pending(d, until=T + 200)
    assert all(media_of(env, i) == 1 for i in ids)
    out = m.correct(d, ids[:2], False)                                                       # one click re-decides the rest
    assert set(ids[2:]) <= set(out["retagged"]) and all(media_of(env, i) == 0 for i in ids[2:])
    assert all(media_of(env, i) == -1 for i in ids[:2])                                     # Fred's own clicks


def test_call_mode_is_room_only(env):
    d, m = env["db"], env["media"]
    w = caller(env)
    tvwindows(env, T, T + 400, source="mic:vic")
    env["calls"].record(d, T - 5, T + 400, "zoom", "auto")                                  # the call is on gus
    ids = [line(env, T + i * 20, 18, w, source="mic:vic") for i in range(15)]
    m.retag(d, ids)                                   # remote-mic lines are decided after diarization (insert stores 0)
    assert sum(media_of(env, i) == 1 for i in ids) >= 10                                     # vic's TV is still vic's TV


def test_media_log_skips_call_time(env):
    d, m = env["db"], env["media"]
    tvwindows(env, T, T + 120)
    assert m.sessions(d, T - 10, T + 200)
    env["calls"].record(d, T - 5, T + 130, "zoom", "auto")
    assert m.sessions(d, T - 10, T + 200) == []


# --- naming ------------------------------------------------------------------------------------------------------

def test_media_context_respects_fred_and_calls(env):
    d, dz = env["db"], env["diarize"]
    w = caller(env)
    tvwindows(env, T, T + 100)
    a = line(env, T + 10, 18, w)
    assert dz.media_context(d, a) is True
    d.execute("UPDATE segments SET media=-1 WHERE id=?", (a,))
    assert dz.media_context(d, a) is False                                                   # Fred said talk
    b = line(env, T + 40, 18, w)
    env["calls"].record(d, T - 5, T + 100, "zoom", "auto")
    assert dz.media_context(d, b) is False                                                   # call mode


# --- picker ------------------------------------------------------------------------------------------------------

def test_repeat_pick_is_a_no_op(env):
    d, spk, dbm = env["db"], env["spk"], env["dbm"]
    fred = dbm.new_speaker(d, "Fred", 1)
    jordan = dbm.new_speaker(d, "Jordan", 1)
    sid = line(env, T, 4.0, fred)
    first = spk.reassign(d, sid, speaker_id=jordan, who="test")
    assert first["batch_id"] and not first.get("unchanged")
    n = d.execute("SELECT COUNT(*) FROM audit WHERE segment_id=?", (sid,)).fetchone()[0]
    for _ in range(6):                                                                       # line 298: 7 saves in 4 s
        again = spk.reassign(d, sid, speaker_id=jordan, who="test")
        assert again["unchanged"] and again["speaker_id"] == jordan
    assert d.execute("SELECT COUNT(*) FROM audit WHERE segment_id=?", (sid,)).fetchone()[0] == n
    row = d.execute("SELECT speaker_id, manual FROM segments WHERE id=?", (sid,)).fetchone()
    assert (row["speaker_id"], row["manual"]) == (jordan, 1)
    back = spk.reassign(d, sid, speaker_id=fred, who="test")                                 # a real change still applies
    assert not back.get("unchanged") and d.execute("SELECT speaker_id FROM segments WHERE id=?", (sid,)).fetchone()[0] == fred


def test_picker_guards_repeat_clicks():
    """The picker shows the choice at once and ignores clicks while the save runs (no JS test runner: check the code)."""
    from pathlib import Path
    s = (Path(__file__).resolve().parent.parent / "ramblr/static/index.html").read_text()
    i = s.index("function pickerDlg(")
    body = s[i:s.index("let reviewTimer", i)]
    assert "if(busy)return;busy=true;" in body and "b.disabled=true" in body and "'saving…'" in body
    assert "busy=false;btns.forEach(b=>b.disabled=false)" in body                            # a failed save re-enables
