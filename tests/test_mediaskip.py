"""mediaskip (Fred 2026-10-25): media lines get no speaker labels, never teach a voiceprint and never found a Speaker-N
cluster; existing ones are unlabelled by a restorable migration; NOT MEDIA diarizes them afresh and undoes as one
batch; search, exports and ideas leave them out. Reference: the 12:23 PM Oct 25 stretch of a YouTube video was full
of Speaker 9/11/12/13 labels on TV dialogue. Temp DB only (conftest)."""
import importlib
import time
from datetime import datetime

import numpy as np
import pytest

DIM = 16
T = datetime(2026, 10, 25, 12, 23).timestamp()


def unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


def mix(a, b, cos):
    b = b - (b @ a) * a
    b = b / np.linalg.norm(b)
    return unit(cos * a + np.sqrt(1 - cos ** 2) * b)


@pytest.fixture
def env(monkeypatch):
    from ramblr import config, db, diarize, media, mediaskip, speakers
    for m in (db, speakers, media, mediaskip, diarize):
        importlib.reload(m)
    monkeypatch.setattr(config, "SPK_NAMED_MIN_N", 1)      # one hand sample is enough to name in these tests
    d = db.connect(config.DB_PATH)
    e = np.eye(DIM, dtype=np.float32)
    fred = db.new_speaker(d, "Fred", 1)

    def line(vec, ts, dur=4.0, media_flag=0, manual_sp=None, text="and then the tree came down", diar=True):
        sid, _ = db.insert_segment(d, ts, dur, text, None, "t", 1)
        d.execute("UPDATE segments SET media=? WHERE id=?", (media_flag, sid))
        if manual_sp:
            d.execute("UPDATE segments SET manual=1, speaker_id=? WHERE id=?", (manual_sp, sid))
        if diar:
            eid = d.execute("INSERT INTO embeddings(segment_id,dim,vec,model) VALUES(?,?,?,?)", (sid, DIM, db.vec_to_blob(vec), "t")).lastrowid
            diarize.assign(d, sid, dur, vec, eid)
        return sid
    line(e[0], T - 600, manual_sp=fred)
    return {"d": d, "db": db, "dz": diarize, "media": media, "ms": mediaskip, "spk": speakers, "cfg": config,
            "line": line, "e": e, "fred": fred}


def seg(env, sid):
    return env["d"].execute("SELECT * FROM segments WHERE id=?", (sid,)).fetchone()


def n_speakers(env):
    return env["d"].execute("SELECT COUNT(*) FROM speakers").fetchone()[0]


def tv(env, t0, t1, level):
    t = t0
    while t < t1:
        env["d"].execute("INSERT INTO media_windows(ts0,ts1,music,tv,crowd,narration,speech,top,rms) VALUES(?,?,0,?,0,0,0.6,0,0.05)",
                         (t, t + 2.88, level))
        t += 2.88


def test_media_lines_get_no_speaker_no_sample_and_found_no_cluster(env):
    d, e, line = env["d"], env["e"], env["line"]
    before = n_speakers(env)
    ids = [line(mix(e[3], e[4 + k], 0.97), T + k * 10, media_flag=1) for k in range(5)]      # one steady TV voice
    ids.append(line(mix(e[3], e[10], 0.97), T + 60, media_flag=2))                           # marked TV by hand
    for sid in ids:
        r = seg(env, sid)
        assert r["speaker_id"] is None and r["cluster_id"] is None and r["diar"] == 1 and r["emb_id"]
    assert n_speakers(env) == before                                    # was: a new Speaker-N, then 5 more lines on it
    assert d.execute(f"SELECT COUNT(*) FROM embeddings WHERE counted_for IS NOT NULL AND segment_id IN ({','.join('?' * len(ids))})",
                     ids).fetchone()[0] == 0
    sk = d.execute("SELECT COUNT(*) n, SUM(labeled) l, SUM(voice) v FROM media_skips WHERE ended IS NULL").fetchone()
    assert (sk["n"], sk["l"], sk["v"]) == (6, 0, 6)                    # the voice it would have had, kept for the classifier
    talk = line(mix(e[3], e[11], 0.97), T + 300)                        # the same voice as talk is still labelled
    assert seg(env, talk)["speaker_id"] is not None and n_speakers(env) == before + 1


def test_a_line_that_turns_tv_once_its_voice_is_read_founds_nothing(env):
    """Talk-over vs media is decided with the voice: an unknown voice over weak TV is media. The diarizer now asks
    before it labels, so that line never founds a cluster or teaches one (the old code did both, then retagged)."""
    d, e, line, media = env["d"], env["e"], env["line"], env["media"]
    tv(env, T + 1000, T + 1012, 0.32)                                   # alone p 0.43; with an unknown voice p 0.58
    before = n_speakers(env)
    sid = line(unit(e[7]), T + 1002)
    r = seg(env, sid)
    assert r["media"] == 1 and r["speaker_id"] is None and n_speakers(env) == before
    assert media.retag(d, [sid]) == [] and seg(env, sid)["media"] == 1  # stable: the kept voice gives the same answer


def labelled_the_old_way(env, vec, ts, cluster, media_flag):
    """A line as the old code left it: speaker + cluster set and its voice counted into the cluster's print."""
    d, db = env["d"], env["db"]
    sid, _ = db.insert_segment(d, ts, 4.0, "the old way", None, "t", 1)
    eid = d.execute("INSERT INTO embeddings(segment_id,dim,vec,model) VALUES(?,?,?,?)", (sid, DIM, db.vec_to_blob(vec), "t")).lastrowid
    d.execute("UPDATE segments SET speaker_id=?, cluster_id=?, sim=0.81, emb_id=?, diar=1, media=? WHERE id=?", (cluster, cluster, eid, media_flag, sid))
    db.add_to_voiceprint(d, cluster, eid, vec)
    return sid


def test_unlabelling_keeps_the_media_decision(env):
    d, e, media, ms, db = env["d"], env["e"], env["media"], env["ms"], env["db"]
    cl = db.new_speaker(d)
    tv(env, T, T + 30, 0.32)
    sid = labelled_the_old_way(env, unit(e[5]), T + 5, cl, 1)
    p0 = media.decide(d, d.execute(f"SELECT {media.SEG_COLS} FROM segments WHERE id=?", (sid,)).fetchone())[1]
    assert ms.sync(d, [sid], "test")["unlabelled"] == [sid]
    p1 = media.decide(d, d.execute(f"SELECT {media.SEG_COLS} FROM segments WHERE id=?", (sid,)).fetchone())[1]
    assert seg(env, sid)["speaker_id"] is None and abs(p0 - p1) < 1e-9 and media.retag(d, [sid]) == []


def test_migration_unlabels_existing_media_lines_deletes_nothing_and_restores_exactly(env):
    d, e, ms, db = env["d"], env["e"], env["ms"], env["db"]
    cl = db.new_speaker(d)
    tvs = [labelled_the_old_way(env, mix(e[6], e[7 + k], 0.95), T + k * 10, cl, 1 + k % 2) for k in range(4)]
    talk = labelled_the_old_way(env, mix(e[6], e[12], 0.95), T + 100, cl, 0)
    vp0 = d.execute("SELECT vec_sum, n FROM voiceprints WHERE speaker_id=?", (cl,)).fetchone()
    texts0 = d.execute("SELECT id, text FROM segments ORDER BY id").fetchall()
    dry = ms.migrate(d)
    assert not dry["applied"] and dry["unlabelled"] == 4 and seg(env, tvs[0])["speaker_id"] == cl     # dry run: nothing
    res = ms.migrate(d, apply=True)
    assert res["unlabelled"] == 4 and res["media_lines_with_speaker_left"] == 0 and res["samples_out"] == {cl: 4}
    for t in ("segments", "embeddings", "speakers", "voiceprints", "audit", "media_corrections"):
        assert res["rows_after"][t] == res["rows_before"][t]                                          # no row deleted
    assert res["rows_after"]["media_skips"] == res["rows_before"]["media_skips"] + 4
    assert [tuple(r) for r in d.execute("SELECT id, text FROM segments ORDER BY id")] == [tuple(r) for r in texts0]
    assert all(seg(env, s)["speaker_id"] is None for s in tvs) and seg(env, talk)["speaker_id"] == cl
    k = d.execute("SELECT * FROM media_skips WHERE segment_id=? AND ended IS NULL", (tvs[0],)).fetchone()
    assert (k["speaker_id"], k["cluster_id"], k["sim"], k["labeled"], k["counted_for"], k["batch_id"]) == (cl, cl, 0.81, 1, cl, res["batch"])
    assert d.execute("SELECT n FROM voiceprints WHERE speaker_id=?", (cl,)).fetchone()[0] == 1          # only the talk line
    assert ms.migrate(d, apply=True)["unlabelled"] == 0                                               # idempotent
    back = ms.restore_batch(d, res["batch"], apply=True)
    assert back["restored"] == 4 and all(seg(env, s)["speaker_id"] == cl and seg(env, s)["sim"] == 0.81 for s in tvs)
    vp1 = d.execute("SELECT vec_sum, n FROM voiceprints WHERE speaker_id=?", (cl,)).fetchone()
    assert vp1["n"] == vp0["n"] and np.allclose(db.blob_to_vec(vp1["vec_sum"]), db.blob_to_vec(vp0["vec_sum"]), atol=1e-5)


def test_not_media_labels_the_lines_for_real_and_undo_puts_everything_back(env):
    d, e, line, media, ms = env["d"], env["e"], env["line"], env["media"], env["ms"]
    fred = env["fred"]
    ids = [line(mix(e[0], e[5 + k], 0.97), T + k * 8, media_flag=1) for k in range(3)]   # Fred, wrongly called TV
    assert all(seg(env, s)["speaker_id"] is None for s in ids)
    w0 = media.load_model(d)["w"]
    n0 = d.execute("SELECT n FROM voiceprints WHERE speaker_id=?", (fred,)).fetchone()[0]
    r = ms.not_media(d, ids)
    assert r["undo"] and r["labelled"] == 3 and r["named"] == 3
    assert all(seg(env, s)["media"] == -1 and seg(env, s)["speaker_id"] == fred for s in ids)
    assert d.execute("SELECT COUNT(*) FROM media_corrections WHERE undone_at IS NULL").fetchone()[0] == 3
    assert d.execute("SELECT COUNT(*) FROM media_skips WHERE ended IS NULL").fetchone()[0] == 0
    back = ms.undo_not_media(d, r["undo"])
    assert sorted(back) == sorted(ids)
    for s in ids:
        x = seg(env, s)
        assert x["media"] == 1 and x["speaker_id"] is None and x["cluster_id"] is None
    assert d.execute("SELECT COUNT(*) FROM media_skips WHERE ended IS NULL").fetchone()[0] == 3        # parked again
    assert d.execute("SELECT COUNT(*) FROM media_corrections WHERE undone_at IS NOT NULL").fetchone()[0] == 3   # kept, inert
    w1 = media.load_model(d)["w"]
    assert all(abs(w1[k] - w0[k]) < 1e-6 for k in w0)                                                # the model is back
    assert d.execute("SELECT n FROM voiceprints WHERE speaker_id=?", (fred,)).fetchone()[0] == n0
    assert ms.undo_not_media(d, r["undo"]) == []                                                      # one undo only


def test_classifier_flip_back_to_talk_restores_the_old_label(env):
    d, e, ms, db = env["d"], env["e"], env["ms"], env["db"]
    cl = db.new_speaker(d)
    sid = labelled_the_old_way(env, unit(e[9]), T, cl, 0)
    d.execute("UPDATE segments SET media=1 WHERE id=?", (sid,))
    ms.sync(d, [sid], "retag")
    assert seg(env, sid)["speaker_id"] is None
    d.execute("UPDATE segments SET media=0 WHERE id=?", (sid,))
    assert ms.sync(d, [sid], "retag")["relabelled"] == [sid]
    x = seg(env, sid)
    assert (x["speaker_id"], x["cluster_id"], x["sim"]) == (cl, cl, 0.81)
    assert d.execute("SELECT counted_for FROM embeddings WHERE id=?", (x["emb_id"],)).fetchone()[0] == cl


def test_heal_takes_a_speaker_back_off_a_media_line(env):
    d, e, line, ms = env["d"], env["e"], env["line"], env["ms"]
    sid = line(unit(e[8]), T, media_flag=1)
    d.execute("UPDATE segments SET speaker_id=? WHERE id=?", (env["fred"], sid))           # e.g. an old relabel undone
    assert ms.heal(d)["unlabelled"] == [sid] and seg(env, sid)["speaker_id"] is None
    assert ms.heal(d) == {"unlabelled": [], "relabelled": []}


def test_fred_relabelling_a_talk_line_never_propagates_onto_media_lines(env):
    d, e, line, spk = env["d"], env["e"], env["line"], env["spk"]
    talk = line(mix(e[0], e[13], 0.6), T)                                # an unnamed voice Fred is about to claim
    tvl = line(mix(e[0], e[14], 0.97), T + 20, media_flag=1)             # sounds just like Fred, but it is the TV
    spk.reassign(d, talk, speaker_id=env["fred"])
    assert seg(env, talk)["speaker_id"] == env["fred"] and seg(env, tvl)["speaker_id"] is None


def test_the_diarizer_waits_for_the_media_tag(env, tmp_path):
    import soundfile as sf
    d, e, dz, cfg = env["d"], env["e"], env["dz"], env["cfg"]
    cfg.SPOOL.mkdir(parents=True, exist_ok=True)
    sf.write(cfg.SPOOL / "a.wav", np.zeros(16000, np.float32), 16000)
    sid, _ = env["db"].insert_segment(d, time.time() - 5, 1.5, "a fresh line", "a.wav", "t", 1)

    class Emb:
        def embed(self, samples):
            return unit(e[15])
    assert dz.run_batch(d, Emb()) == 0                                   # not tagged yet: wait (up to DIAR_MEDIA_WAIT)
    d.execute("UPDATE segments SET media=2 WHERE id=?", (sid,))           # marked TV (an auto 1 with no TV heard flips back)
    assert dz.run_batch(d, Emb()) == 1
    assert seg(env, sid)["speaker_id"] is None and seg(env, sid)["diar"] == 1


def test_search_exports_and_ideas_skip_media_lines(env):
    from fastapi.testclient import TestClient
    from ramblr import web
    importlib.reload(web)
    d, e, line = env["d"], env["e"], env["line"]
    day = datetime.fromtimestamp(T).strftime("%Y-%m-%d")
    a = line(mix(e[0], e[5], 0.97), T, text="the budget plan for the shop")
    b = line(unit(e[9]), T + 30, media_flag=1, text="the budget plan on the evening news")
    c = TestClient(web.app)
    assert [x["id"] for x in c.get("/api/segments", params={"q": "budget"}).json()] == [a]
    assert {x["id"] for x in c.get("/api/segments", params={"q": "budget", "media": 1}).json()} == {a, b}
    assert {x["id"] for x in c.get("/api/segments", params={"day": day}).json()} >= {a, b}      # the day view folds them
    rng = {"start": datetime.fromtimestamp(T - 60).strftime("%Y-%m-%d %H:%M"), "end": datetime.fromtimestamp(T + 120).strftime("%Y-%m-%d %H:%M")}
    txt = c.get("/api/export", params=rng).text
    assert "shop" in txt and "evening news" not in txt
    assert "evening news" in c.get("/api/export", params={**rng, "media": 1}).text
    md = c.get("/api/export.md", params={"day": day}).text
    assert "shop" in md and "evening news" not in md
    iid = c.post("/api/ideas", json={"segment_ids": [a, b]}).json()["idea_id"]
    assert [r[0] for r in d.execute("SELECT segment_id FROM idea_lines WHERE idea_id=?", (iid,))] == [a]
    r = c.post("/api/segments/media", json={"segment_ids": [b], "media": False}).json()
    assert r["undo"] and seg(env, b)["media"] == -1 and seg(env, b)["speaker_id"] is not None
    u = c.post("/api/media/notmedia/undo", json={"batch": r["undo"]}).json()
    assert u["restored"] == 1
    assert seg(env, b)["media"] == 1 and seg(env, b)["speaker_id"] is None
