"""retagscope (2026-10-25): a media click changes only the lines it was made on. Fred marked 169 lines (11:58-12:48, a
YouTube video YAMNet heard as plain speech, its voices inside the family clusters) as TV in one click; the refit and
the day-wide re-decide then flipped 52 room-talk lines (09:14-11:51, 12:54-13:02) to TV with no undo, and the new
weights went on calling the family's new lines TV. A "not media" on 7 lines of a TV stretch likewise flipped the rest
of the stretch to talk. Temp DB only (conftest)."""
import importlib
from datetime import datetime

import pytest

T9 = datetime(2026, 10, 25, 9, 0).timestamp()
T12 = datetime(2026, 10, 25, 11, 58).timestamp()


@pytest.fixture
def env():
    from ramblr import config, db, diarize, media, mediaskip, speakers
    for m in (db, speakers, media, mediaskip, diarize):
        importlib.reload(m)
    d = db.connect(config.DB_PATH)
    return {"d": d, "db": db, "media": media, "ms": mediaskip, "A": db.new_speaker(d), "B": db.new_speaker(d)}


def line(env, t, cluster, media_flag=0, dur=2.0, text="so we moved the shop to Atlanta"):
    sid, _ = env["db"].insert_segment(env["d"], t, dur, text, None, "t", 1)
    env["d"].execute("UPDATE segments SET speaker_id=?, cluster_id=?, diar=1, media=? WHERE id=?", (cluster, cluster, media_flag, sid))
    return sid


def row(env, sid):
    return env["d"].execute("SELECT * FROM segments WHERE id=?", (sid,)).fetchone()


def family_day(env):
    """Room talk in the morning and after lunch; between them a video that sounds like people talking, its voices
    landing in the same two family clusters (the BRIO puts every voice in a narrow cone)."""
    A, B = env["A"], env["B"]
    morning = [line(env, T9 + k * 30, A if k % 2 else B) for k in range(20)]
    video = [line(env, T12 + k * 12, A if k % 2 else B) for k in range(60)]
    after = [line(env, T12 + 56 * 60 + k * 30, A if k % 2 else B) for k in range(6)]
    return morning, video, after


def test_a_tv_click_never_flips_talk_outside_the_selection(env):
    d, media = env["d"], env["media"]
    morning, video, after = family_day(env)
    out = media.correct(d, video, True, "test")
    talk = morning + after
    assert [row(env, s)["media"] for s in talk] == [0] * len(talk)                  # was: all 26 flipped to TV
    assert all(row(env, s)["speaker_id"] is not None for s in talk)                 # ... and lost their speaker
    assert out["retagged"] == [] and out["spread"] == [] and out["batch"] is None
    assert all(row(env, s)["media"] == 2 and row(env, s)["speaker_id"] is None for s in video)


def test_a_big_range_click_does_not_brand_the_family_voices_tv(env):
    """One click is one example: the weights barely move, and the per-voice "Fred marked this voice TV" ratio needs
    repeated clicks, not one range. A new room line from the same voice after the click is still talk."""
    d, media = env["d"], env["media"]
    morning, video, after = family_day(env)
    media.correct(d, video, True, "test")
    for k, cl in enumerate((env["A"], env["B"], env["A"])):
        sid, _ = env["db"].insert_segment(d, T12 + 90 * 60 + k * 30, 2.0, "are you coming to the game", None, "t", 1)
        d.execute("UPDATE segments SET speaker_id=?, cluster_id=?, diar=1 WHERE id=?", (cl, cl, sid))
        m, p, f = media.decide(d, row(env, sid))
        assert m == 0 and p < 0.5 and f["unknown_media_ratio"] == 0, (k, p, f)     # was: p 0.9+, ratio 1.0


def test_not_media_on_part_of_a_tv_stretch_leaves_the_rest_alone(env):
    d, media, ms = env["d"], env["media"], env["ms"]
    t = T12
    while t < T12 + 130:
        d.execute("INSERT INTO media_windows(ts0,ts1,music,tv,crowd,narration,speech,top,rms) VALUES(?,?,0,0.9,0,0,0.6,0,0.05)", (t, t + 2.88))
        t += 2.88
    tv = [line(env, T12 + k * 10, env["A"], media_flag=1, dur=8.0, text="and the satellites relay it") for k in range(10)]
    ms.sync(d, tv, "test")
    out = ms.not_media(d, tv[3:6], "test")
    assert [row(env, s)["media"] for s in tv] == [1, 1, 1, -1, -1, -1, 1, 1, 1, 1]  # was: 7-10 flipped to talk too
    assert out["retagged"] == [] and all(row(env, s)["speaker_id"] is None for s in tv[6:])
    ms.undo_not_media(d, out["undo"])
    assert [row(env, s)["media"] for s in tv] == [1] * 10
