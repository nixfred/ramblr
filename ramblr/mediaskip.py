"""Media lines carry no speaker (Fred, 2026-10-25): a TV / music line is never a person, so it gets no label.

- The diarizer never labels a media line (auto or hand-marked TV), never counts its voice into a voiceprint and never
  founds a Speaker-N cluster from it. The embedding is still stored, and media_skips keeps the voice it would have
  picked, so the media decision (which looks at who spoke: a named voice over the TV is talk) stays what it was.
- A line that becomes media later (a retag, Fred's "that was the TV", a spread) has its speaker taken off: the old
  values go to media_skips and its sample leaves the voiceprint. heal() sweeps up any media line still carrying one,
  and migrate() is the same sweep over the whole DB, once, as one batch that restore_batch() can put back.
- NOT MEDIA (Fred says it was talk): the lines become -1, the classifier retrains, and they are diarized afresh from
  their stored embeddings so they get real labels. not_media() returns an undo batch; undo_not_media() puts the media
  flags, the speakers and the model back. An automatic flip back to talk restores the speaker the line showed before.
Nothing is deleted: media_skips is append-only, segment rows are only updated."""
import json
import time
import uuid

from . import db as dbm

MEDIA = (1, 2)


def _tx(db, fn):
    if db.in_transaction:
        return fn()
    db.execute("BEGIN IMMEDIATE")
    try:
        out = fn()
        db.execute("COMMIT")
        return out
    except BaseException:
        db.execute("ROLLBACK")
        raise


def open_skip(db, seg_id):
    return db.execute("SELECT * FROM media_skips WHERE segment_id=? AND ended IS NULL", (seg_id,)).fetchone()


def voice_of(db, seg_id):
    """(speaker_id, cluster_id, voice) a speakerless media line had or would have had; None if it has no record."""
    r = open_skip(db, seg_id)
    return (r["speaker_id"], r["cluster_id"], bool(r["voice"])) if r else None


def _close(db, skip_id, how):
    db.execute("UPDATE media_skips SET ended=?, ended_how=? WHERE id=?", (time.time(), how, skip_id))


def _emb(db, emb_id):
    return db.execute("SELECT id, vec, counted_for FROM embeddings WHERE id=?", (emb_id,)).fetchone() if emb_id else None


def _live(db, sid):
    """sid if that speaker row exists and was not merged away, else None."""
    if sid is None:
        return None
    r = db.execute("SELECT merged_into FROM speakers WHERE id=?", (sid,)).fetchone()
    return sid if r and not r["merged_into"] else None


def _speaker(db, sid, cluster):
    """The person to show again: sid if it still exists unmerged, else whoever its voice resolves to now (or None)."""
    from . import speakers
    if _live(db, sid) is not None:
        return sid
    r = speakers.resolve(db, sid if sid is not None else cluster)
    return r if r is not None and db.execute("SELECT 1 FROM speakers WHERE id=?", (r,)).fetchone() else None


def unlabel(db, seg_id, why, batch=None):
    """Take the speaker off one media line: values into media_skips, its sample out of the voiceprint. True if changed."""
    s = db.execute("SELECT id, media, speaker_id, cluster_id, sim, manual, emb_id FROM segments WHERE id=?", (seg_id,)).fetchone()
    if not s or s["media"] not in MEDIA:
        return False
    e = _emb(db, s["emb_id"])
    counted = e["counted_for"] if e else None
    if s["speaker_id"] is None and s["cluster_id"] is None and counted is None:
        return False
    old = open_skip(db, seg_id)
    if old:
        _close(db, old["id"], "superseded")        # a speaker was put back on a skipped line meanwhile: keep the newer one
    db.execute("""INSERT INTO media_skips(segment_id, speaker_id, cluster_id, sim, manual, counted_for, labeled, voice, why,
                  batch_id, created) VALUES(?,?,?,?,?,?,1,?,?,?,?)""",
               (seg_id, s["speaker_id"], s["cluster_id"], s["sim"], s["manual"], counted,
                1 if (s["speaker_id"] or s["cluster_id"]) else 0, why, batch, time.time()))
    if counted is not None:
        dbm.remove_from_voiceprint(db, e["id"], dbm.blob_to_vec(e["vec"]))
    db.execute("UPDATE segments SET speaker_id=NULL, cluster_id=NULL, sim=NULL, manual=0 WHERE id=?", (seg_id,))
    return True


def skip_new(db, seg_id, speaker_id, cluster_id, sim, voice, why="diarize"):
    """A media line the diarizer did not label: remember the voice it would have picked (labeled=0, nothing learned)."""
    if open_skip(db, seg_id):
        return
    db.execute("""INSERT INTO media_skips(segment_id, speaker_id, cluster_id, sim, manual, counted_for, labeled, voice, why, created)
                  VALUES(?,?,?,?,0,NULL,0,?,?,?)""", (seg_id, speaker_id, cluster_id, sim, 1 if voice else 0, why, time.time()))


def _put_back(db, seg_id, sk):
    """Exactly the speaker values a line showed before it was unlabelled (a voice merged away since: re-resolved)."""
    sp = _speaker(db, sk["speaker_id"], sk["cluster_id"])
    db.execute("UPDATE segments SET speaker_id=?, cluster_id=?, sim=?, manual=? WHERE id=?",
               (sp, sk["cluster_id"], sk["sim"], sk["manual"] if sp is not None else 0, seg_id))
    e = _emb(db, db.execute("SELECT emb_id FROM segments WHERE id=?", (seg_id,)).fetchone()[0])
    to = _live(db, sk["counted_for"])
    if e is not None and to is not None:
        dbm.add_to_voiceprint(db, to, e["id"], dbm.blob_to_vec(e["vec"]))
    return sp


def fresh(db, seg_id):
    """Diarize one line again from its stored embedding (no audio needed), as the diarizer would for talk."""
    from . import diarize, speakers
    s = db.execute("SELECT id, dur, emb_id FROM segments WHERE id=?", (seg_id,)).fetchone()
    e = _emb(db, s["emb_id"]) if s else None
    if e is None:
        return None                                # not diarized yet: the diarizer labels it when it gets there
    v = dbm.blob_to_vec(e["vec"])
    dbm.remove_from_voiceprint(db, e["id"], v)
    cl, sim = diarize.decide(db, seg_id, s["dur"], v, e["id"])
    who = speakers.resolve(db, cl) if cl is not None else None
    db.execute("UPDATE segments SET speaker_id=?, cluster_id=?, sim=?, manual=0, diar=1 WHERE id=?", (who, cl, sim, seg_id))
    return who


def relabel(db, seg_id, is_fresh, how):
    """A skipped line is talk again. is_fresh (Fred said not media): diarize it anew; otherwise put back the speaker it
    really showed before (a would-be-only record is diarized anew). Returns the speaker id or None."""
    s = db.execute("SELECT id, media, speaker_id FROM segments WHERE id=?", (seg_id,)).fetchone()
    sk = open_skip(db, seg_id)
    if not s or s["media"] in MEDIA or not sk:
        return None
    _close(db, sk["id"], how)
    if s["speaker_id"] is not None:
        return s["speaker_id"]                     # labelled by hand meanwhile: that wins
    if not is_fresh and sk["labeled"]:
        return _put_back(db, seg_id, sk)
    return fresh(db, seg_id)


def sync(db, ids, why="sync", batch=None):
    """After media flags changed: media lines lose their speaker; lines that are talk again get one back (fresh if Fred
    marked them not media, -1; the old one if the classifier changed its mind). {"unlabelled": [...], "relabelled": [...]}"""
    out = {"unlabelled": [], "relabelled": []}
    ids = sorted({int(i) for i in ids or []})
    if not ids:
        return out

    def run():
        for sid in ids:
            r = db.execute("SELECT media FROM segments WHERE id=?", (sid,)).fetchone()
            if not r:
                continue
            if r["media"] in MEDIA:
                if unlabel(db, sid, why, batch):
                    out["unlabelled"].append(sid)
            elif open_skip(db, sid) is not None:
                relabel(db, sid, r["media"] == -1, why)
                out["relabelled"].append(sid)
        return out
    return _tx(db, run)


def heal(db, limit=500, why="heal", batch=None):
    """Sweep: any media line still carrying a speaker, cluster or voiceprint sample, and any talk line still parked."""
    ids = [r[0] for r in db.execute("""SELECT s.id FROM segments s LEFT JOIN embeddings e ON e.id=s.emb_id WHERE s.media IN (1,2)
                                       AND (s.speaker_id IS NOT NULL OR s.cluster_id IS NOT NULL OR e.counted_for IS NOT NULL)
                                       ORDER BY s.id LIMIT ?""", (limit,))]
    ids += [r[0] for r in db.execute("""SELECT k.segment_id FROM media_skips k JOIN segments s ON s.id=k.segment_id
                                        WHERE k.ended IS NULL AND COALESCE(s.media,0) NOT IN (1,2) LIMIT ?""", (limit,))]
    return sync(db, ids, why, batch)


def table_counts(db):
    return {r[0]: db.execute(f'SELECT COUNT(*) FROM "{r[0]}"').fetchone()[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'segments_fts%' ORDER BY name")}


def migrate(db, apply=False):
    """The one-time sweep of existing media lines, as one batch (restore_batch puts it back). Dry run unless apply."""
    batch = "mediaskip-" + time.strftime("%Y%m%d-%H%M%S")
    before = table_counts(db)
    vp0 = dict(db.execute("SELECT speaker_id, n FROM voiceprints").fetchall())
    db.execute("BEGIN IMMEDIATE")
    try:
        res = heal(db, limit=10 ** 9, why="migration", batch=batch)
        vp1 = dict(db.execute("SELECT speaker_id, n FROM voiceprints").fetchall())
        left = db.execute("SELECT COUNT(*) FROM segments WHERE media IN (1,2) AND speaker_id IS NOT NULL").fetchone()[0]
        if apply:
            dbm.bump_rev(db)
        db.execute("COMMIT" if apply else "ROLLBACK")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    after = table_counts(db)
    return {"applied": apply, "batch": batch if apply else None, "unlabelled": len(res["unlabelled"]),
            "relabelled": len(res["relabelled"]), "media_lines_with_speaker_left": left,
            "samples_out": {k: vp0[k] - vp1.get(k, 0) for k in vp0 if vp0[k] != vp1.get(k, 0)},
            "rows_before": before, "rows_after": after}


def restore_batch(db, batch, apply=False):
    """Undo a migration batch: every line it unlabelled gets its exact old speaker and sample back (media unchanged)."""
    def run():
        n = 0
        for sk in db.execute("SELECT * FROM media_skips WHERE batch_id=? AND ended IS NULL AND labeled=1", (batch,)).fetchall():
            if db.execute("SELECT speaker_id FROM segments WHERE id=?", (sk["segment_id"],)).fetchone()[0] is None:
                _put_back(db, sk["segment_id"], sk)
                n += 1
            _close(db, sk["id"], "restored")
        return n
    db.execute("BEGIN IMMEDIATE")
    try:
        n = run()
        if apply:
            dbm.bump_rev(db)
        db.execute("COMMIT" if apply else "ROLLBACK")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return {"applied": apply, "restored": n}


SNAP = "id, day, media, media_p, speaker_id, cluster_id, sim, manual, emb_id"


def not_media(db, ids, who="site"):
    """NOT MEDIA: the lines are talk (-1, a training example), the day is re-decided, and each line is diarized afresh
    so it gets a real label. One undo batch covers it all (undo_not_media)."""
    from . import media
    ids = sorted({int(i) for i in ids})
    if not ids:
        raise ValueError("no lines")
    ph = ",".join("?" * len(ids))
    snap = {r["id"]: dict(r) for r in db.execute(f"SELECT {SNAP} FROM segments WHERE id IN ({ph})", ids)}
    if len(snap) != len(ids):
        raise LookupError("unknown line id")
    for sid, s in snap.items():
        e = _emb(db, s["emb_id"])
        s["counted_for"] = e["counted_for"] if e else None
        k = open_skip(db, sid)
        s["skip"] = k["id"] if k else None
    days = sorted({s["day"] for s in snap.values()})
    dph = ",".join("?" * len(days))
    day_media = {r[0]: (r[1], r[2]) for r in db.execute(f"SELECT id, media, media_p FROM segments WHERE day IN ({dph}) AND media IN (0,1)", days)}
    c0 = db.execute("SELECT COALESCE(MAX(id),0) FROM media_corrections").fetchone()[0]
    out = media.correct(db, ids, False, who)        # -1, refit, re-decide the day; skipped lines are diarized afresh
    corr = dict(db.execute(f"SELECT segment_id, id FROM media_corrections WHERE id>? AND segment_id IN ({ph})", [c0] + ids).fetchall())
    batch = uuid.uuid4().hex[:12]
    now = time.time()

    def run():
        labels = {}
        for sid in ids:
            s = snap[sid]
            cur = db.execute("SELECT speaker_id, manual FROM segments WHERE id=?", (sid,)).fetchone()
            if s["skip"] is None and s["media"] in MEDIA and not cur["manual"]:
                fresh(db, sid)                     # a media line not swept yet (still had its old label): a fresh look too
            cur = db.execute("SELECT speaker_id FROM segments WHERE id=?", (sid,)).fetchone()
            labels[sid] = cur["speaker_id"]
            db.execute("""INSERT INTO audit(batch_id, kind, segment_id, from_speaker, to_speaker, detail, who, created)
                          VALUES(?, 'not_media', ?, ?, ?, ?, ?, ?)""",
                       (batch, sid, s["speaker_id"], cur["speaker_id"], json.dumps({**{k: s[k] for k in s if k not in ("id", "day")},
                                                                                   "correction": corr.get(sid)}), who, now))
        for rid in out["retagged"]:
            new = db.execute("SELECT media FROM segments WHERE id=?", (rid,)).fetchone()[0]
            old, old_p = day_media.get(rid, (None, None))
            db.execute("""INSERT INTO audit(batch_id, kind, segment_id, detail, who, created) VALUES(?, 'not_media_retag', ?, ?, ?, ?)""",
                       (batch, rid, json.dumps({"old": old, "old_p": old_p, "new": new}), who, now))
        dbm.bump_rev(db)
        return labels
    labels = _tx(db, run)
    named = {r[0] for r in db.execute("SELECT id FROM speakers WHERE named=1")}
    return {**out, "undo": batch, "labelled": sum(1 for v in labels.values() if v is not None),
            "named": sum(1 for v in labels.values() if v in named), "speakers": labels}


def undo_not_media(db, batch, who="site"):
    """Put one NOT MEDIA click back: the model (its corrections are marked undone and the weights refit), the media flags,
    and the speaker values each line had (a media line: none, its media_skips record reopened). Returns the line ids."""
    from . import media
    rows = db.execute("""SELECT * FROM audit WHERE batch_id=? AND kind IN ('not_media','not_media_retag') AND undone_at IS NULL
                         ORDER BY id""", (batch,)).fetchall()
    if not rows:
        return []
    now = time.time()

    def run():
        back = []
        corr = [json.loads(a["detail"]).get("correction") for a in rows if a["kind"] == "not_media"]
        corr = [c for c in corr if c]
        if corr:
            db.execute(f"UPDATE media_corrections SET undone_at=? WHERE id IN ({','.join('?' * len(corr))})", [now] + corr)
        media.fit(db)
        for a in rows:
            d, sid = json.loads(a["detail"]), a["segment_id"]
            if a["kind"] == "not_media":
                s = db.execute("SELECT media, emb_id FROM segments WHERE id=?", (sid,)).fetchone()
                if s and s["media"] == -1:                 # nobody changed it since
                    e = _emb(db, s["emb_id"])
                    if e is not None and e["counted_for"] is not None:
                        dbm.remove_from_voiceprint(db, e["id"], dbm.blob_to_vec(e["vec"]))
                    k = open_skip(db, sid)
                    if k:
                        _close(db, k["id"], "undo")
                    db.execute("UPDATE segments SET media=?, media_p=?, speaker_id=?, cluster_id=?, sim=?, manual=? WHERE id=?",
                               (d["media"], d["media_p"], _speaker(db, d["speaker_id"], d["cluster_id"]) if d["speaker_id"] else None,
                                d["cluster_id"], d["sim"], d["manual"], sid))
                    to = _live(db, d["counted_for"])
                    if e is not None and to is not None:
                        dbm.add_to_voiceprint(db, to, e["id"], dbm.blob_to_vec(e["vec"]))
                    if d.get("skip"):
                        db.execute("""INSERT INTO media_skips(segment_id, speaker_id, cluster_id, sim, manual, counted_for, labeled,
                                      voice, why, batch_id, created) SELECT segment_id, speaker_id, cluster_id, sim, manual,
                                      counted_for, labeled, voice, 'undo', ?, ? FROM media_skips WHERE id=?""", (batch, now, d["skip"]))
                    back.append(sid)
            elif db.execute("UPDATE segments SET media=?, media_p=? WHERE id=? AND media IS ?", (d["old"], d.get("old_p"), sid, d["new"])).rowcount:
                sync(db, [sid], "undo", batch)
                back.append(sid)
            db.execute("UPDATE audit SET undone_at=? WHERE id=?", (now, a["id"]))
        dbm.bump_rev(db)
        return back
    return _tx(db, run)


if __name__ == "__main__":     # python -m ramblr.mediaskip migrate [--apply] | restore BATCH [--apply] | status
    import sys
    from . import config
    d = dbm.connect(config.DB_PATH)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "migrate":
        print(json.dumps(migrate(d, apply="--apply" in sys.argv), indent=1))
    elif cmd == "restore":
        print(json.dumps(restore_batch(d, sys.argv[2], apply="--apply" in sys.argv), indent=1))
    else:
        print(json.dumps({"media_lines": d.execute("SELECT COUNT(*) FROM segments WHERE media IN (1,2)").fetchone()[0],
                          "media_lines_with_speaker": d.execute("SELECT COUNT(*) FROM segments WHERE media IN (1,2) AND speaker_id IS NOT NULL").fetchone()[0],
                          "open_skips": d.execute("SELECT COUNT(*) FROM media_skips WHERE ended IS NULL").fetchone()[0]}, indent=1))
