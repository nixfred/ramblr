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


def test_retroactive_record_moves_the_start_back(env):
    c, d = env["calls"], env["db"]
    tick = c.record(d, T + 2000, T + 2005, "call switch", "manual")                        # capture's first tick after "on"
    assert c.record(d, T, T + 2005, "call switch", "manual") == tick                         # the earlier window, recorded later
    r = d.execute("SELECT t0, t1 FROM calls WHERE id=?", (tick,)).fetchone()
    assert (r["t0"], r["t1"]) == (T, T + 2005) and c.during(d, T + 10, T + 20)


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
    out = m.correct(d, ids[:2], False)
    assert all(media_of(env, i) == -1 for i in ids[:2])                                     # Fred's own clicks
    # retagscope (2026-10-25): a click changes only its own lines (the day-wide re-decide flipped 52 room-talk lines
    # after one TV click). The caller's stretch is covered two other ways: NOT MEDIA on the 'Media skipped' row is the
    # whole stretch, and the carried "not TV" keeps every new line of the stretch talk.
    assert out["retagged"] == [] and all(media_of(env, i) == 1 for i in ids[2:])
    nxt = line(env, T + 6 * 20, 18, w)
    m.tag_pending(d, until=T + 200)
    assert media_of(env, nxt) == 0                                                           # carried: talk
    m.correct(d, ids[2:], False)                                                             # the row's NOT MEDIA
    assert all(media_of(env, i) == -1 for i in ids)


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


# --- round 2: site switch, heartbeat, voiceprint safety ------------------------------------------------------------

def test_call_status_signals_and_heartbeat(env):
    c, d = env["calls"], env["db"]
    assert c.status(d)["live"] is False
    cid, ttl = c.heartbeat(d, "zoom", 9999)
    assert ttl == 180.0                                                                       # capped
    s = c.status(d)
    assert s["live"] and s["signals"] == ["heartbeat"] and s["heartbeat_app"] == "zoom"
    assert c.during(d, time.time() + 10, time.time() + 20)                                   # covers the next lines
    env["state"].update(by="t", call_until=time.time() + 3600)
    assert c.status(d)["signals"] == ["switch", "heartbeat"]
    env["dbm"].set_meta(d, "capture_call", "brave")
    assert c.status(d)["signals"] == ["switch", "heartbeat", "gus"]


def test_router_call_endpoint_needs_its_own_key(env, tmp_path, monkeypatch):
    monkeypatch.setenv("RAMBLR_CALL_KEY", "call-test-key")
    monkeypatch.setenv("RAMBLR_VOICE_ROUTER_KEY", "router-test-key")
    from ramblr import config, router
    importlib.reload(config); importlib.reload(router)
    from fastapi.testclient import TestClient
    cl = TestClient(router.make_app(tmp_path / "r.db"))
    assert cl.post("/call", json={"ttl": 60, "app": "zoom"}).status_code == 401
    assert cl.post("/call", json={"ttl": 60}, headers={"Authorization": "Bearer router-test-key"}).status_code == 401   # not the router key
    r = cl.post("/call", json={"ttl": 60, "app": "zoom"}, headers={"Authorization": "Bearer call-test-key"})
    assert r.status_code == 200 and r.json()["ttl"] == 60
    assert env["calls"].status(env["dbm"].connect(config.DB_PATH))["heartbeat_app"] == "zoom"


def test_web_call_switch(env, monkeypatch):
    from ramblr import config, web
    importlib.reload(web)
    from fastapi.testclient import TestClient
    cl = TestClient(web.app)
    r = cl.post("/api/call", json={"on": True}).json()
    assert r["live"] and r["signals"] == ["switch"]
    assert abs(r["switch_until"] - (time.time() + config.CALL_SWITCH_HOURS * 3600)) < 60     # holds; 4 h safety net
    assert cl.get("/api/status").json()["call"]["live"] is True
    assert cl.post("/api/call", json={"on": False}).json()["live"] is False


def vec(seed, base=None, mix=0.0):
    rng = np.random.default_rng(seed)
    v = rng.normal(size=256)
    v /= np.linalg.norm(v)                            # unit noise, so `mix` really is the share of noise
    if base is not None:
        v = (1 - mix) * base + mix * v
    return (v / np.linalg.norm(v)).astype(np.float32)


def voiced(env, t, spk, v, manual=0):
    sid = line(env, t, 4.0, spk)
    d, dbm = env["db"], env["dbm"]
    e = d.execute("INSERT INTO embeddings(segment_id,dim,vec,model) VALUES(?,?,?,?)", (sid, len(v), dbm.vec_to_blob(v), "t")).lastrowid
    d.execute("UPDATE segments SET emb_id=?, manual=? WHERE id=?", (e, manual, sid))
    return sid, e


def test_thin_named_print_never_names_on_its_own(env):
    from ramblr import config
    d, dbm, dz = env["db"], env["dbm"], env["diarize"]
    a = vec(1)
    person = dbm.new_speaker(d, "Sam", 1)
    for i in range(config.SPK_NAMED_MIN_N - 1):                                              # 7 samples: thin
        _, e = voiced(env, T + i, person, vec(100 + i, a, 0.05), manual=1)
        dbm.add_to_voiceprint(d, person, e, dbm.blob_to_vec(d.execute("SELECT vec FROM embeddings WHERE id=?", (e,)).fetchone()[0]))
    sid, e = voiced(env, T + 50, None, vec(200, a, 0.02))
    v = dbm.blob_to_vec(d.execute("SELECT vec FROM embeddings WHERE id=?", (e,)).fetchone()[0])
    cl, sim = dz.decide(d, sid, 4.0, v, e, learn=False)
    assert cl != person and sim > 0.95                                                       # very close, but the print is thin
    _, e8 = voiced(env, T + 60, person, vec(300, a, 0.05), manual=1)
    dbm.add_to_voiceprint(d, person, e8, dbm.blob_to_vec(d.execute("SELECT vec FROM embeddings WHERE id=?", (e8,)).fetchone()[0]))
    cl, _ = dz.decide(d, sid, 4.0, v, e, learn=False)
    assert cl == person                                                                      # 8 samples: names it


def test_ambiguous_manual_sample_counts_half_and_is_flagged(env):
    from ramblr import config
    d, dbm, spk = env["db"], env["dbm"], env["spk"]
    a, b = vec(1), vec(2)
    sam, jordan = dbm.new_speaker(d, "Sam", 1), dbm.new_speaker(d, "Jordan", 1)
    for i in range(3):
        for who, base in ((sam, a), (jordan, b)):
            _, e = voiced(env, T + i * 10 + (who == jordan), who, vec(10 * who + i, base, 0.1), manual=1)
            dbm.add_to_voiceprint(d, who, e, dbm.blob_to_vec(d.execute("SELECT vec FROM embeddings WHERE id=?", (e,)).fetchone()[0]))
    clear, _ = voiced(env, T + 100, None, vec(50, a, 0.1))
    mixed, _ = voiced(env, T + 110, None, (a + b) / np.linalg.norm(a + b))                   # sounds like both
    spk.reassign(d, clear, speaker_id=sam, who="t", propagate=False)
    spk.reassign(d, mixed, speaker_id=sam, who="t", propagate=False)
    w = {r["segment_id"]: (r["weight"], r["ambiguous"]) for r in d.execute("SELECT segment_id, weight, ambiguous FROM embeddings")}
    assert w[clear] == (config.MANUAL_WEIGHT, 0)
    assert w[mixed] == (config.AMBIGUOUS_WEIGHT, 1)
    assert d.execute("SELECT speaker_id FROM segments WHERE id=?", (mixed,)).fetchone()[0] == sam   # Fred's word still wins
    from ramblr import web
    importlib.reload(web)
    p = web.person_core(d, d.execute("SELECT * FROM speakers WHERE id=?", (sam,)).fetchone())
    assert [x["segment_id"] for x in p["ambiguous_samples"]] == [mixed]


def test_merge_suggestion_one_click(env):
    from ramblr import config
    d, dbm, spk = env["db"], env["dbm"], env["spk"]
    a, b = vec(1), vec(2)
    sam, jordan = dbm.new_speaker(d, "Sam", 1), dbm.new_speaker(d, "Jordan", 1)
    for who, base in ((sam, a), (jordan, b)):
        _, e = voiced(env, T + who, who, vec(who, base, 0.1), manual=1)
        dbm.add_to_voiceprint(d, who, e, dbm.blob_to_vec(d.execute("SELECT vec FROM embeddings WHERE id=?", (e,)).fetchone()[0]))
    cl = dbm.new_speaker(d)                                                                  # "Speaker N": really Jordan
    for i in range(config.MERGE_MIN_LINES):
        _, e = voiced(env, T + 100 + i * 5, cl, vec(500 + i, b, 0.15))
        dbm.add_to_voiceprint(d, cl, e, dbm.blob_to_vec(d.execute("SELECT vec FROM embeddings WHERE id=?", (e,)).fetchone()[0]))
    small = dbm.new_speaker(d)                                                               # too few lines: no suggestion
    for i in range(3):
        _, e = voiced(env, T + 300 + i * 5, small, vec(700 + i, b, 0.15))
        dbm.add_to_voiceprint(d, small, e, dbm.blob_to_vec(d.execute("SELECT vec FROM embeddings WHERE id=?", (e,)).fetchone()[0]))
    sg = spk.suggestions(d)
    assert [(s["cluster_id"], s["person_id"]) for s in sg] == [(cl, jordan)] and sg[0]["sim"] - sg[0]["next"]["sim"] >= config.MERGE_MARGIN
    spk.dismiss_suggestion(d, cl, jordan)
    assert spk.suggestions(d) == []                                                          # "No" is remembered
    d.execute("UPDATE meta SET value='[]' WHERE key='merge_dismissed'")
    spk.rename(d, cl, "Jordan", who="t")                                                    # "Yes, merge" = the existing merge
    assert d.execute("SELECT merged_into FROM speakers WHERE id=?", (cl,)).fetchone()[0] == jordan
    assert spk.suggestions(d) == []
