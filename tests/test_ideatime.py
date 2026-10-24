"""B4-2/7/8/10: idea heat from repeat mentions with decay, lineage links, weekly review, X drafts never posted."""
import json
import time

import numpy as np
import pytest


@pytest.fixture
def env(monkeypatch):
    import importlib
    from ramblr import config, db, ideatime, ideas, drafts
    for m in (db, ideas, ideatime, drafts):
        importlib.reload(m)
    return {"db": db.connect(config.DB_PATH), "dbm": db, "t": ideatime, "ideas": ideas, "dr": drafts}


def unit(seed):
    v = np.random.default_rng(seed).normal(size=768).astype(np.float32)
    return v / np.linalg.norm(v)


def test_heat_mentions_decay_lineage_review(env):
    d, dbm, t = env["db"], env["dbm"], env["t"]
    now = time.time()
    old = now - 40 * 86400
    s1, _ = dbm.insert_segment(d, old, 3, "a dog door idea", None, "t", 1)
    s2, _ = dbm.insert_segment(d, old + 10, 3, "a pizza idea", None, "t", 1)
    a, _ = env["ideas"].create(d, [s1], "range")
    b, _ = env["ideas"].create(d, [s2], "range")
    d.execute("UPDATE ideas SET created=?, updated=? WHERE id IN (?,?)", (old, old, a, b))
    va, vb = unit(1), unit(2)
    vc = (va * 0.9 + unit(3) * 0.1); vc /= np.linalg.norm(vc)              # a later pitch of the same idea
    s3, _ = dbm.insert_segment(d, now - 86400, 3, "the dog door again", None, "t", 1)
    c, _ = env["ideas"].create(d, [s3], "range")
    for iid, v in ((a, va), (b, vb), (c, vc)):
        d.execute("UPDATE ideas SET vec=? WHERE id=?", (dbm.vec_to_blob(v), iid))
    d.execute("INSERT INTO chunks(first_seg,last_seg,day,ts0,ts1,seg_ids,vec,model) VALUES(?,?,?,?,?,?,?,?)",
              (s3, s3, "2026-10-23", now - 86400, now - 86397, json.dumps([s3]), dbm.vec_to_blob(vc), "t"))
    t.score(d, now=now)
    h = {r["id"]: (r["heat"], r["mentions"]) for r in d.execute("SELECT id, heat, mentions FROM ideas")}
    assert h[a][1] == 1 and h[b][1] == 0                    # the dog door came up again; pizza did not
    assert h[a][0] > h[b][0]                                # repeated idea climbs, ignored one fades
    assert t.lineage(d, a)[0]["id"] == c                    # the later pitch is linked to the first
    d.execute("UPDATE ideas SET status='later', updated=? WHERE id=?", (now, b))
    assert [x["id"] for x in t.review(d)] == [b]


def test_x_drafts(env, monkeypatch):
    d, dbm, dr = env["db"], env["dbm"], env["dr"]
    s, _ = dbm.insert_segment(d, time.time(), 3, "NPUs are underrated for always-on audio", None, "t", 1)
    monkeypatch.setattr(dr.llm, "generate", lambda p, **k: (json.dumps({"drafts": [{"body": "NPUs are underrated for always-on audio.", "why": "rant"}]}), "test", "stub", 0, ""))
    assert dr.make_x_post(d, segment_ids=[s]) == {"made": 1}
    assert d.execute("SELECT kind, status FROM drafts").fetchone()[:] == ("x-post", "draft")
