"""Reassign / bulk / merge / propagation / undo on a throwaway DB with crafted embeddings (exact cosines)."""
import math
import sqlite3
import time

import numpy as np
import pytest

from ramblr import config, db as dbm, diarize, speakers as sp

DIM = 256


def unit(i):
    v = np.zeros(DIM, dtype=np.float32)
    v[i] = 1.0
    return v


U, V = unit(0), unit(1)


def towards(c, k):
    """A unit vector with cosine exactly c to U, the rest along private axis k (orthogonal to U and V)."""
    return (c * U + math.sqrt(max(0.0, 1 - c * c)) * unit(k)).astype(np.float32)


@pytest.fixture
def world(tmp_path):
    d = dbm.connect(tmp_path / "t.db")
    C = dbm.new_speaker(d)                      # unnamed diarizer cluster "Speaker 1"
    D = dbm.new_speaker(d)                      # another unnamed cluster
    Q = dbm.new_speaker(d, "Jordan", named=1)  # a named person
    day, t0 = "2026-10-24", time.mktime((2026, 10, 24, 10, 0, 0, 0, 0, -1))
    L = {}

    def line(name, vec, speaker, cluster, manual=0, dayv=day, noise=0, k=None):
        sid, _ = dbm.insert_segment(d, t0 + len(L) * 10, 2.0, f"{name} text", None, "test", 1)
        d.execute("UPDATE segments SET day=?, noise=?, diar=1 WHERE id=?", (dayv, noise, sid))
        eid = d.execute("INSERT INTO embeddings(segment_id,dim,vec,model) VALUES(?,?,?,?)",
                        (sid, DIM, dbm.vec_to_blob(vec), "t")).lastrowid
        d.execute("UPDATE segments SET speaker_id=?, cluster_id=?, manual=?, emb_id=? WHERE id=?", (speaker, cluster, manual, eid, sid))
        dbm.add_to_voiceprint(d, cluster if cluster else speaker, eid, vec)
        L[name] = sid

    line("L0", U, C, C)                       # Fred reassigns this one
    line("L1", towards(0.90, 2), C, C)        # high score -> auto
    line("L2", towards(0.70, 3), C, C)        # same unnamed cluster, >= 0.65 -> auto
    line("L3", towards(0.70, 4), D, D)        # other cluster, 0.65-0.78 -> flagged
    line("L4", towards(0.50, 5), D, D)        # low -> untouched
    line("L5", V, Q, Q, manual=1)             # manual -> never touched
    line("L6", (0.8 * U + 0.6 * V).astype(np.float32), Q, Q)   # 0.80 to Fred but closer to Jordan -> untouched
    line("L7", towards(0.60, 6), C, C)        # same cluster but below 0.65 -> flagged
    line("L8", towards(0.99, 7), C, C, dayv="2026-10-23")      # other day -> untouched
    line("N1", towards(0.99, 8), C, C, noise=1)                # noise -> untouched
    return d, L, dict(C=C, D=D, Q=Q)


def count(d):
    return d.execute("SELECT COUNT(*) FROM segments").fetchone()[0]


def spk(d, sid):
    return d.execute("SELECT speaker_id, manual FROM segments WHERE id=?", (sid,)).fetchone()


def test_propagation_applies_skips_flags_and_undo(world):
    d, L, S = world
    n0 = count(d)
    r = sp.reassign(d, L["L0"], name="Fred")
    F = r["speaker_id"]
    prop = r["propagation"]
    applied = {x["id"] for x in prop["applied"]}
    flagged = {x["id"] for x in prop["flagged"]}
    assert applied == {L["L1"], L["L2"]}
    assert flagged == {L["L3"], L["L7"]}
    assert spk(d, L["L0"])["speaker_id"] == F and spk(d, L["L0"])["manual"] == 1
    for k in ("L1", "L2"):
        assert spk(d, L[k])["speaker_id"] == F and spk(d, L[k])["manual"] == 0
    for k, who in (("L3", S["D"]), ("L4", S["D"]), ("L5", S["Q"]), ("L6", S["Q"]), ("L7", S["C"]), ("L8", S["C"]), ("N1", S["C"])):
        assert spk(d, L[k])["speaker_id"] == who, k
    assert spk(d, L["L5"])["manual"] == 1
    assert count(d) == n0
    # Fred's voiceprint is exactly L0's direction; Speaker 1's no longer holds L0
    assert np.allclose(sp.centroid(d, F), U, atol=1e-5)
    # future lines: the cluster now maps to Fred
    assert sp.resolve(d, S["C"]) == F
    # undo only the propagation batch
    u = sp.undo(d, prop["batch_id"])
    assert u["reverted"] == 2
    for k in ("L1", "L2"):
        assert spk(d, L[k])["speaker_id"] == S["C"]
    assert spk(d, L["L0"])["speaker_id"] == F           # the manual pick survives a propagation undo
    assert sp.resolve(d, S["C"]) == S["C"]               # cluster mapping withdrawn
    assert d.execute("SELECT COUNT(*) FROM suggestions WHERE state='open'").fetchone()[0] == 0
    # undo the reassign itself: original speaker and manual flag back, voiceprint back to Speaker 1
    sp.undo(d, r["batch_id"])
    assert spk(d, L["L0"])["speaker_id"] == S["C"] and spk(d, L["L0"])["manual"] == 0
    assert sp.centroid(d, F) is None
    e0 = d.execute("SELECT counted_for, weight FROM embeddings WHERE segment_id=?", (L["L0"],)).fetchone()
    assert e0["counted_for"] == S["C"] and e0["weight"] == 1.0
    assert count(d) == n0


def test_undo_reassign_cascades_to_its_propagation(world):
    d, L, S = world
    r = sp.reassign(d, L["L0"], name="Fred")
    sp.undo(d, r["batch_id"])
    for k in ("L0", "L1", "L2"):
        assert spk(d, L[k])["speaker_id"] == S["C"]


def test_manual_pick_wins_over_later_propagation(world):
    d, L, S = world
    sp.reassign(d, L["L1"], speaker_id=S["Q"], propagate=False)     # Fred says L1 is Jordan
    r = sp.reassign(d, L["L0"], name="Fred")
    assert L["L1"] not in {x["id"] for x in r["propagation"]["applied"]}
    assert spk(d, L["L1"])["speaker_id"] == S["Q"]


def test_accept_suggestion_is_manual_and_undoable(world):
    d, L, S = world
    r = sp.reassign(d, L["L0"], name="Fred")
    g = next(x for x in r["propagation"]["flagged"] if x["id"] == L["L3"])
    a = sp.decide_suggestion(d, g["suggestion_id"], True)
    assert spk(d, L["L3"])["speaker_id"] == r["speaker_id"] and spk(d, L["L3"])["manual"] == 1
    sp.undo(d, a["batch_id"])
    assert spk(d, L["L3"])["speaker_id"] == S["D"]


def test_bulk_reassign_and_undo(world):
    d, L, S = world
    n0 = count(d)
    ids = [L["L3"], L["L4"], L["L7"]]
    before = {i: spk(d, i)["speaker_id"] for i in ids}
    b = sp.bulk(d, ids, speaker_id=S["Q"])
    assert b["lines"] == 3 and all(spk(d, i)["speaker_id"] == S["Q"] for i in ids)
    assert count(d) == n0
    sp.undo(d, b["batch_id"])
    assert {i: spk(d, i)["speaker_id"] for i in ids} == before
    assert count(d) == n0


def test_merge_by_rename_and_undo(world):
    d, L, S = world
    n0 = count(d)
    c_lines = [r[0] for r in d.execute("SELECT id FROM segments WHERE speaker_id=?", (S["C"],))]
    m = sp.rename(d, S["C"], "jordan")                 # existing name, any case -> merge
    assert m["merged"] and m["speaker_id"] == S["Q"]
    assert all(spk(d, i)["speaker_id"] == S["Q"] for i in c_lines)
    assert sp.resolve(d, S["C"]) == S["Q"]
    assert count(d) == n0
    sp.undo(d, m["batch_id"])
    assert all(spk(d, i)["speaker_id"] == S["C"] for i in c_lines)
    assert sp.resolve(d, S["C"]) == S["C"]
    assert sp.centroid(d, S["C"]) is not None
    assert count(d) == n0


def test_rename_color_confirm_undo_and_contrast(world):
    d, L, S = world
    r = sp.rename(d, S["D"], "Tom")
    assert d.execute("SELECT name, named FROM speakers WHERE id=?", (S["D"],)).fetchone()[:] == ("Tom", 1)
    sp.undo(d, r["batch_id"])
    assert d.execute("SELECT name, named FROM speakers WHERE id=?", (S["D"],)).fetchone()[:] == ("Speaker 2", 0)
    with pytest.raises(ValueError):
        sp.set_color(d, S["Q"], "#222222")             # unreadable on near-black
    c = sp.set_color(d, S["Q"], "#ffcb6b")
    assert sp.color_for(S["Q"], 1, d.execute("SELECT color FROM speakers WHERE id=?", (S["Q"],)).fetchone()[0]) == "#ffcb6b"
    sp.undo(d, c["batch_id"])
    assert d.execute("SELECT color FROM speakers WHERE id=?", (S["Q"],)).fetchone()[0] is None
    k = sp.confirm(d, S["Q"])                          # temp DB only, never the live one
    assert d.execute("SELECT confirmed FROM speakers WHERE id=?", (S["Q"],)).fetchone()[0] == 1
    sp.undo(d, k["batch_id"])
    assert d.execute("SELECT confirmed FROM speakers WHERE id=?", (S["Q"],)).fetchone()[0] == 0


def test_colors_persistent_and_gray_for_unnamed():
    assert sp.color_for(7, 1) == sp.color_for(7, 1)
    assert sp.color_for(7, 0) == sp.GRAY and sp.color_for(None, 0) == sp.GRAY
    assert all(sp.contrast(c) >= 4.5 for c in sp.PALETTE + [sp.GRAY])


def test_diarizer_follows_cluster_map_for_new_lines(world):
    d, L, S = world
    r = sp.reassign(d, L["L0"], name="Fred")
    sid, _ = dbm.insert_segment(d, time.time(), 2.0, "new line", "x.wav", "test", 1)
    cvec = sp.centroid(d, S["C"])                       # sounds exactly like Speaker 1's cluster
    eid = d.execute("INSERT INTO embeddings(segment_id,dim,vec,model) VALUES(?,?,?,?)", (sid, DIM, dbm.vec_to_blob(cvec), "t")).lastrowid
    who = diarize.assign(d, sid, 2.0, cvec, eid)
    row = d.execute("SELECT speaker_id, cluster_id FROM segments WHERE id=?", (sid,)).fetchone()
    assert row["cluster_id"] == S["C"] and row["speaker_id"] == r["speaker_id"] == who


def test_lines_and_audit_cannot_be_deleted(world):
    d, L, S = world
    with pytest.raises(sqlite3.DatabaseError):
        d.execute("DELETE FROM segments WHERE id=?", (L["L0"],))
    sp.reassign(d, L["L0"], name="Fred")
    with pytest.raises(sqlite3.DatabaseError):
        d.execute("DELETE FROM audit")
