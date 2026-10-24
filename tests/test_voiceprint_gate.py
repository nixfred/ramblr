"""E1 (Fred 7:57 AM): 120 of 125 lines were tagged Fred because his voiceprint learned from every match and became
the room average. Named voices are now gated (SPK_NAMED_MATCH + SPK_MARGIN), learn only from very sure, long,
media-free lines, and `redo` re-decides old lines from stored embeddings without touching manual ones. Temp DB."""
import importlib
import json

import numpy as np
import pytest

DIM = 16


def unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


def mix(a, b, cos):
    """A unit vector with cosine `cos` to a, leaning toward b."""
    b = b - (b @ a) * a
    b = b / np.linalg.norm(b)
    return unit(cos * a + np.sqrt(1 - cos ** 2) * b)


@pytest.fixture
def env():
    from ramblr import config, db, diarize, speakers
    for m in (db, speakers, diarize):
        importlib.reload(m)
    d = db.connect(config.DB_PATH)
    e = np.eye(DIM, dtype=np.float32)
    fred, cry = db.new_speaker(d, "Fred", 1), db.new_speaker(d, "Jordan", 1)
    F, C = e[0], mix(e[0], e[1], 0.80)              # the two voices sit 0.80 apart, like on gus's mic

    def line(vec, ts, dur=4.0, manual_sp=None, text="x"):
        sid, _ = db.insert_segment(d, ts, dur, text, None, "t", 1)
        eid = d.execute("INSERT INTO embeddings(segment_id,dim,vec,model) VALUES(?,?,?,?)", (sid, DIM, db.vec_to_blob(vec), "t")).lastrowid
        if manual_sp:
            d.execute("UPDATE segments SET manual=1, speaker_id=? WHERE id=?", (manual_sp, sid))
        diarize.assign(d, sid, dur, vec, eid)
        return sid
    line(F, 100, manual_sp=fred)
    line(C, 110, manual_sp=cry)
    return {"d": d, "db": db, "dz": diarize, "line": line, "F": F, "C": C, "e": e, "fred": fred, "cry": cry}


def n_print(env, sp):
    return env["d"].execute("SELECT n FROM voiceprints WHERE speaker_id=?", (sp,)).fetchone()[0]


def test_room_average_voices_are_not_fred_and_never_poison_his_print(env):
    d, F, C, e = env["d"], env["F"], env["C"], env["e"]
    room = unit(F + C)                                                                       # ~0.95 to both: no margin
    ids = [env["line"](mix(room, e[2 + k % 10], 0.97), 200 + k * 10) for k in range(40)]
    sp = [r[0] for r in d.execute(f"SELECT speaker_id FROM segments WHERE id IN ({','.join('?' * len(ids))})", ids)]
    assert env["fred"] not in sp and n_print(env, env["fred"]) == 1                          # was: all Fred, print n=41


def test_clear_fred_line_is_fred_and_learns_only_when_sure_long_and_quiet(env):
    d, F, e = env["d"], env["F"], env["e"]
    short = env["line"](mix(F, e[5], 0.97), 300, dur=1.0)
    assert d.execute("SELECT speaker_id FROM segments WHERE id=?", (short,)).fetchone()[0] == env["fred"]
    assert n_print(env, env["fred"]) == 1                                                     # too short to teach
    d.execute("INSERT INTO media_windows(ts0,ts1,music,tv,crowd,narration,speech,top,rms) VALUES(395,420,0,0.9,0,0,0,518,0.1)")
    tv = env["line"](mix(F, e[6], 0.97), 400, dur=5.0)
    assert d.execute("SELECT speaker_id FROM segments WHERE id=?", (tv,)).fetchone()[0] != env["fred"] and n_print(env, env["fred"]) == 1   # TV on: never named, never learned
    env["line"](mix(F, e[7], 0.97), 500, dur=5.0)
    assert n_print(env, env["fred"]) == 2                                                     # sure, long, no TV: learns


def test_jordan_learns_from_confirmed_lines_and_close_calls_stay_unnamed(env):
    d, F, C, e = env["d"], env["F"], env["C"], env["e"]
    close = env["line"](unit(F + C), 600)                                                     # halfway: no margin
    assert d.execute("SELECT speaker_id FROM segments WHERE id=?", (close,)).fetchone()[0] not in (env["fred"], env["cry"])
    clear = env["line"](mix(C, e[8], 0.97), 700)
    assert d.execute("SELECT speaker_id FROM segments WHERE id=?", (clear,)).fetchone()[0] == env["cry"]


def test_redo_repairs_a_poisoned_print_and_never_touches_manual_lines(env):
    d, db, dz, F, C, e = env["d"], env["db"], env["dz"], env["F"], env["C"], env["e"]
    ids = []
    for k in range(20):                     # simulate the old behaviour: jordan-ish lines stamped Fred and summed into his print
        v = mix(C, e[3 + k % 8], 0.95)
        sid, _ = db.insert_segment(d, 1000 + k * 10, 4.0, "old", None, "t", 1)
        eid = d.execute("INSERT INTO embeddings(segment_id,dim,vec,model) VALUES(?,?,?,?)", (sid, DIM, db.vec_to_blob(v), "t")).lastrowid
        db.add_to_voiceprint(d, env["fred"], eid, v)
        d.execute("UPDATE segments SET speaker_id=?, cluster_id=?, emb_id=?, diar=1 WHERE id=?", (env["fred"], env["fred"], eid, sid))
        ids.append(sid)
    assert n_print(env, env["fred"]) == 21
    dry = dz.redo(d, 0)
    assert dry["applied"] is False and n_print(env, env["fred"]) == 21                         # dry run changes nothing
    out = dz.redo(d, 0, apply=True)
    assert out["purged_from_named"] == 20 and n_print(env, env["fred"]) == 1
    sp = [r[0] for r in d.execute(f"SELECT speaker_id FROM segments WHERE id IN ({','.join('?' * len(ids))})", ids)]
    assert sp.count(env["cry"]) == 20                                                          # they were Jordan all along
    man = d.execute("SELECT speaker_id, manual FROM segments WHERE manual=1 ORDER BY id").fetchall()
    assert [tuple(r) for r in man] == [(env["fred"], 1), (env["cry"], 1)]
    assert d.execute("SELECT COUNT(*) FROM audit WHERE kind='rediarize'").fetchone()[0] == out["changed"] > 0
