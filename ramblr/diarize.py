"""Batch speaker labelling on CPU: wespeaker ResNet34 ONNX embeddings (ungated, no token).
Runs light (nice 19, 1 thread) and only touches segments that are new. Deletes each spool wav once embedded."""
import logging
import sys
import time

import numpy as np
import soundfile as sf

from . import config, db as dbm, media, notify, speakers

log = logging.getLogger("ramblr.diarize")


class Embedder:
    def __init__(self):
        import sherpa_onnx as so
        cfg = so.SpeakerEmbeddingExtractorConfig(model=str(config.SPK_MODEL), num_threads=1, debug=False, provider="cpu")
        self.ex = so.SpeakerEmbeddingExtractor(cfg)
        self.dim = self.ex.dim

    def embed(self, samples):
        st = self.ex.create_stream()
        st.accept_waveform(sample_rate=config.RATE, waveform=samples)
        st.input_finished()
        v = np.array(self.ex.compute(st), dtype=np.float32)
        n = np.linalg.norm(v)
        return v / n if n > 0 else v


def assign(db, seg_id, dur, emb, vec_emb_id):
    """Pick a speaker for one segment from the saved voiceprints (named ones included)."""
    cur = db.execute("SELECT manual, speaker_id FROM segments WHERE id=?", (seg_id,)).fetchone()
    if cur["manual"]:
        # Fred already set this line by hand: keep his speaker, let the voice teach that voiceprint (manual weight).
        db.execute("UPDATE embeddings SET weight=? WHERE id=?", (config.MANUAL_WEIGHT, vec_emb_id))
        if cur["speaker_id"] is not None:
            dbm.add_to_voiceprint(db, cur["speaker_id"], vec_emb_id, emb)
        db.execute("UPDATE segments SET emb_id=?, diar=1, audio=NULL WHERE id=?", (vec_emb_id, seg_id))
        return cur["speaker_id"]
    speaker, best_sim = decide(db, seg_id, dur, emb, vec_emb_id)
    # speaker is the diarizer's cluster; the line shows the person that cluster now belongs to (merges, Fred's picks)
    who = speakers.resolve(db, speaker) if speaker is not None else None
    db.execute("UPDATE segments SET speaker_id=?, cluster_id=?, sim=?, emb_id=?, diar=1, audio=NULL WHERE id=?",
               (who, speaker, best_sim, vec_emb_id, seg_id))
    return who


def media_context(db, seg_id):
    """True if the room TV / music was audible or a Cast device was playing during the line: never learn a voice then."""
    r = db.execute("SELECT ts, dur, source, media FROM segments WHERE id=?", (seg_id,)).fetchone()
    if not r:
        return False
    a, b = r["ts"], r["ts"] + max(r["dur"], 0.3)
    if r["media"] == -1:
        return False                    # Fred said this line is talk, not the TV
    from . import calls
    if r["source"] is None and calls.during(db, a, b):
        return False                    # call mode: the voice out of the speakers is a caller
    m = db.execute("SELECT AVG(MAX(tv, music)) FROM media_windows WHERE ts1>? AND ts0<? AND source IS ?", (a, b, r["source"])).fetchone()[0]
    from . import players
    return bool((m or 0) > 0.3 or (r["source"] is None and players.cast_playing(db, a, b)))


def decide(db, seg_id, dur, emb, vec_emb_id, learn=True):
    """(cluster speaker id or None, best similarity). Named people are gated: a line goes to a named voiceprint only
    with SPK_NAMED_MATCH and a SPK_MARGIN lead over every other named person; otherwise it joins (or founds) an unnamed
    cluster. Named voiceprints learn only from very sure, long, media-free lines, so one person's print cannot
    swallow the whole room (2026-10-24: Fred's print had absorbed 130 lines incl. Jordan, kids and TV)."""
    named_people = {r[0] for r in db.execute("SELECT id FROM speakers WHERE named=1 AND merged_into IS NULL")}
    scored = []
    for sid, c in dbm.voiceprints(db):
        scored.append((float(np.dot(c, emb)), sid, speakers.resolve(db, sid)))
    if not scored:
        if dur >= config.SPK_MIN_SECONDS:
            sp = dbm.new_speaker(db)
            dbm.add_to_voiceprint(db, sp, vec_emb_id, emb)
            return sp, None
        return None, None
    scored.sort(reverse=True)
    best_sim = scored[0][0]
    named = [x for x in scored if x[2] in named_people]
    if named:
        s, sid, person = named[0]
        rival = max((x[0] for x in named if x[2] != person), default=-1.0)   # vs other NAMED people: an unnamed
        # room-average cluster sits near everyone and must not block naming (it never gets a name by accident either)
        quiet = not media_context(db, seg_id)      # a TV anchor resembles whoever's print is thinnest: never name it
        if s >= config.SPK_NAMED_MATCH and s - rival >= config.SPK_MARGIN and quiet:
            if learn and dur >= config.SPK_LEARN_SECONDS and s >= config.SPK_UPDATE and s - rival >= config.SPK_UPDATE_MARGIN:
                dbm.add_to_voiceprint(db, sid, vec_emb_id, emb)
            return sid, s
    unnamed = [x for x in scored if x[2] not in named_people]
    long_enough = dur >= config.SPK_MIN_SECONDS
    if unnamed and unnamed[0][0] >= config.SPK_MATCH:
        if learn and long_enough:
            dbm.add_to_voiceprint(db, unnamed[0][1], vec_emb_id, emb)      # anonymous clusters keep learning
        return unnamed[0][1], unnamed[0][0]
    if long_enough:
        sp = dbm.new_speaker(db)
        if learn:
            dbm.add_to_voiceprint(db, sp, vec_emb_id, emb)
        return sp, best_sim
    return None, best_sim


def purge_auto_from_named(db):
    """Take every auto-learned embedding out of the named people's voiceprints; keep the manual (hand-set) ones."""
    named_people = {r[0] for r in db.execute("SELECT id FROM speakers WHERE named=1 AND merged_into IS NULL")}
    n = 0
    for r in db.execute("""SELECT e.id, e.vec, e.counted_for FROM embeddings e JOIN segments s ON s.id=e.segment_id
                           WHERE e.counted_for IS NOT NULL AND s.manual=0""").fetchall():
        if speakers.resolve(db, r["counted_for"]) in named_people:
            dbm.remove_from_voiceprint(db, r["id"], dbm.blob_to_vec(r["vec"]))
            n += 1
    for p in named_people:
        if db.execute("SELECT 1 FROM voiceprints WHERE speaker_id=?", (p,)).fetchone():
            dbm.recompute_voiceprint(db, p)
    return n


def redo(db, since, apply=False):
    """Re-decide every non-manual line since `since` from its stored embedding (no audio needed). Manual lines are
    never touched. Dry run unless apply=True; apply writes an audit row per changed line. Returns a summary."""
    import json as _json
    import uuid
    before = dict(db.execute("SELECT COALESCE(speaker_id,0), COUNT(*) FROM segments WHERE ts>=? GROUP BY 1", (since,)).fetchall())
    db.execute("BEGIN IMMEDIATE")
    try:
        purged = purge_auto_from_named(db)
        rows = db.execute("""SELECT s.id, s.dur, s.speaker_id, s.cluster_id, e.id AS eid, e.vec FROM segments s JOIN embeddings e ON e.id=s.emb_id
                             WHERE s.ts>=? AND s.manual=0 ORDER BY s.id""", (since,)).fetchall()
        batch, changed = uuid.uuid4().hex[:12], []
        for r in rows:
            v = dbm.blob_to_vec(r["vec"])
            dbm.remove_from_voiceprint(db, r["eid"], v)
            cl, sim = decide(db, r["id"], r["dur"], v, r["eid"])
            who = speakers.resolve(db, cl) if cl is not None else None
            db.execute("UPDATE segments SET speaker_id=?, cluster_id=?, sim=? WHERE id=?", (who, cl, sim, r["id"]))
            if who != r["speaker_id"]:
                changed.append(r["id"])
                db.execute("""INSERT INTO audit(batch_id, kind, segment_id, from_speaker, to_speaker, from_manual, to_manual, score, detail, who, created)
                              VALUES(?, 'rediarize', ?, ?, ?, 0, 0, ?, ?, 'larry', ?)""",
                           (batch, r["id"], r["speaker_id"], who, sim, _json.dumps({"cluster": cl}), time.time()))
        after = dict(db.execute("SELECT COALESCE(speaker_id,0), COUNT(*) FROM segments WHERE ts>=? GROUP BY 1", (since,)).fetchall())
        db.execute("COMMIT" if apply else "ROLLBACK")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    if apply:
        dbm.bump_rev(db)
        media.retag(db, changed)
    return {"applied": apply, "batch": batch if apply else None, "purged_from_named": purged, "lines": len(rows),
            "changed": len(changed), "before": before, "after": after}


def run_batch(db, emb):
    rows = db.execute("SELECT id, dur, audio FROM segments WHERE diar=0 ORDER BY id LIMIT ?", (config.DIAR_BATCH,)).fetchall()
    done = 0
    for r in rows:
        path = config.SPOOL / (r["audio"] or "")
        try:
            samples, rate = sf.read(path, dtype="float32")
        except Exception as e:
            log.error("segment %d: audio unreadable (%s), marking skipped", r["id"], e)
            db.execute("UPDATE segments SET diar=2, audio=NULL WHERE id=?", (r["id"],))
            continue
        v = emb.embed(samples)
        db.execute("BEGIN IMMEDIATE")
        try:
            cur = db.execute("INSERT INTO embeddings(segment_id,dim,vec,model) VALUES(?,?,?,?)",
                             (r["id"], len(v), dbm.vec_to_blob(v), config.SPK_MODEL_NAME))
            sp = assign(db, r["id"], r["dur"], v, cur.lastrowid)
            dbm.bump_rev(db)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        path.unlink(missing_ok=True)       # voice audio is gone as soon as the embedding is stored
        done += 1
        try:
            media.retag(db, [r["id"]])     # now that the voice is known: talk-over vs media is decided again
        except Exception:
            log.exception("media retag failed for %d", r["id"])
        notify.send({"t": "upd", "ids": [r["id"]]})
        log.info("segment %d dur=%.1fs -> speaker %s", r["id"], r["dur"], sp)
    return done


def sweep_orphans(db):
    """Spool files with no pending row (crash leftovers): delete the audio only, never a transcript."""
    pending = {r[0] for r in db.execute("SELECT audio FROM segments WHERE diar=0 AND audio IS NOT NULL")}
    now = time.time()
    for p in config.SPOOL.glob("*.wav"):
        # only this pipeline's own segment clips: never the uploads/ folder (queued iPhone recordings), never
        # music-ID temp files, never anything that is not a regular .wav file at the top level
        if not p.is_file() or p.is_symlink() or p.name.startswith("fp-"):
            continue
        if p.name not in pending and now - p.stat().st_mtime > 600:
            log.warning("removing orphan spool audio %s", p.name)
            p.unlink(missing_ok=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stdout)
    db = dbm.connect(config.DB_PATH)
    emb = Embedder()
    log.warning("diarizer ready: %s dim=%d match>=%.2f", config.SPK_MODEL_NAME, emb.dim, config.SPK_MATCH)
    config.SPOOL.mkdir(parents=True, exist_ok=True)
    last_sweep = 0.0
    while True:
        n = run_batch(db, emb)
        if time.time() - last_sweep > 600:
            last_sweep = time.time()
            sweep_orphans(db)
        if n < config.DIAR_BATCH:
            time.sleep(config.DIAR_INTERVAL)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "redo":      # python -m ramblr.diarize redo --since 2026-10-24 [--apply]
        import argparse
        import json
        from datetime import datetime
        ap = argparse.ArgumentParser()
        ap.add_argument("cmd")
        ap.add_argument("--since", required=True)
        ap.add_argument("--apply", action="store_true")
        a = ap.parse_args()
        print(json.dumps(redo(dbm.connect(config.DB_PATH), datetime.fromisoformat(a.since).timestamp(), apply=a.apply), indent=1))
    else:
        main()
