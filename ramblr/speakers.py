"""People, colors, reassignment, merges, propagation and undo.
Rules: transcript lines are never deleted (only speaker references change); every change is written to the audit
table with a batch id so it can be undone; a line set by hand (manual=1) is never overwritten by propagation or
diarization; nothing marks a voice 'confirmed' except Fred's own click on the site."""
import json
import time
import uuid

import numpy as np

from . import config, db as dbm

PALETTE = ["#f5b85a", "#82aaff", "#ff7a90", "#c3e88d", "#c792ea", "#89ddff",
           "#f78c6c", "#ffcb6b", "#5fd4a0", "#e6a1f0", "#2fb7c9", "#7ff9bc"]   # all >= 7:1 on #0b1220
GRAY = "#8b97ad"                                                              # unnamed "Speaker N" (6.4:1)


def _lum(h):
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(h, bg="#0b1220"):
    a, b = _lum(h), _lum(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def color_for(sid, named, stored=None):
    """One persistent color per person: stored choice, else a deterministic palette pick; unnamed voices are gray."""
    if sid is None:
        return GRAY
    if stored:
        return stored
    if not named:
        return GRAY
    return PALETTE[(int(sid) * 2654435761 % 2 ** 32) % len(PALETTE)]


def new_batch():
    return uuid.uuid4().hex[:12]


def resolve(db, sid, hops=8):
    """Follow merges and cluster->person mappings to the person a voice belongs to now."""
    seen = set()
    while sid is not None and hops and sid not in seen:
        seen.add(sid)
        hops -= 1
        r = db.execute("SELECT merged_into FROM speakers WHERE id=?", (sid,)).fetchone()
        if r and r["merged_into"]:
            sid = r["merged_into"]
            continue
        m = db.execute("SELECT person_id FROM cluster_map WHERE cluster_id=? AND active=1", (sid,)).fetchone()
        if m and m["person_id"] != sid and db.execute("SELECT 1 FROM speakers WHERE id=?", (m["person_id"],)).fetchone():
            # (a mapping to a speaker row that does not exist is ignored: 2026-10-24 cluster 2 -> missing 3)
            sid = m["person_id"]
            continue
        break
    return sid


def centroid(db, sid):
    r = db.execute("SELECT vec_sum FROM voiceprints WHERE speaker_id=?", (sid,)).fetchone() if sid else None
    if not r:
        return None
    v = dbm.blob_to_vec(r["vec_sum"])
    n = np.linalg.norm(v)
    return v / n if n > 0 else None


def person(db, speaker_id=None, name=None):
    """An existing person by id, or by name (case-insensitive), created as a named person if new."""
    if speaker_id is not None:
        r = db.execute("SELECT id FROM speakers WHERE id=? AND merged_into IS NULL", (int(speaker_id),)).fetchone()
        if not r:
            raise LookupError("no such person")
        return r["id"]
    name = " ".join(str(name or "").split())[:60]
    if not name:
        raise ValueError("name required")
    r = db.execute("SELECT id FROM speakers WHERE name=? COLLATE NOCASE AND merged_into IS NULL", (name,)).fetchone()
    return r["id"] if r else dbm.new_speaker(db, name, named=1)


def _audit(db, batch, kind, who, seg=None, frm=None, to=None, fm=None, tm=None, vp=0, eb=None, wb=None,
           score=None, parent=None, detail=None):
    db.execute("""INSERT INTO audit(batch_id,parent_batch,kind,segment_id,from_speaker,to_speaker,from_manual,to_manual,
                  vp_moved,emb_counted_before,emb_weight_before,score,detail,who,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (batch, parent, kind, seg, frm, to, fm, tm, vp, eb, wb, score,
                json.dumps(detail) if detail is not None else None, who, time.time()))


def _move_emb(db, emb_id, to_speaker, weight):
    """Take a line's voice embedding out of whatever voiceprint holds it and count it for to_speaker with weight
    (to_speaker None = count it nowhere). This is the weighted running update and the 'decay from the old person'."""
    e = db.execute("SELECT vec, counted_for FROM embeddings WHERE id=?", (emb_id,)).fetchone()
    if not e:
        return
    vec = dbm.blob_to_vec(e["vec"])
    if e["counted_for"] is not None:
        dbm.remove_from_voiceprint(db, emb_id, vec)
    db.execute("UPDATE embeddings SET weight=? WHERE id=?", (weight, emb_id))
    if to_speaker is not None:
        dbm.add_to_voiceprint(db, to_speaker, emb_id, vec)


def ambiguous_sample(db, emb_id, to):
    """True if this hand-labelled voice sample sounds about as much like another named person as like `to`: within
    SPK_AMBIGUOUS_MARGIN of another named print, compared with `to`'s print without this sample. Fred's word still
    wins (the line keeps his label); the sample just counts AMBIGUOUS_WEIGHT so one mixed clip cannot steer a thin
    print. 2026-10-24: a hand-labelled line scored 0.912 vs 0.907 between the two people on a call, and one caller sample was closer to Fred."""
    e = db.execute("SELECT vec, counted_for, weight FROM embeddings WHERE id=?", (emb_id,)).fetchone()
    if not e:
        return False
    v = dbm.blob_to_vec(e["vec"])
    own, others = None, []
    for r in db.execute("""SELECT v.speaker_id, v.vec_sum, v.n FROM voiceprints v JOIN speakers s ON s.id=v.speaker_id
                           WHERE s.named=1 AND s.merged_into IS NULL"""):
        s = dbm.blob_to_vec(r["vec_sum"])
        n = r["n"]
        if e["counted_for"] == r["speaker_id"]:
            s, n = s - float(e["weight"]) * v, n - 1        # judge against the print without this very sample
        nrm = np.linalg.norm(s)
        if n <= 0 or nrm == 0:
            continue
        sim = float(v @ (s / nrm))
        if resolve(db, r["speaker_id"]) == to:
            own = sim
        else:
            others.append(sim)
    if own is None or not others:
        return False                                       # a first sample, or nobody to confuse it with
    return max(others) >= own - config.SPK_AMBIGUOUS_MARGIN


def manual_weight(db, emb_id, to):
    """(weight, ambiguous flag) for a hand-labelled sample."""
    amb = ambiguous_sample(db, emb_id, to)
    return (config.AMBIGUOUS_WEIGHT if amb else config.MANUAL_WEIGHT), amb


def _manual_set(db, seg, to, batch, kind, who):
    """Set one line by hand: speaker reference + manual flag + voiceprint move. Never touches the text."""
    e = db.execute("SELECT counted_for, weight FROM embeddings WHERE id=?", (seg["emb_id"],)).fetchone() if seg["emb_id"] else None
    _audit(db, batch, kind, who, seg["id"], seg["speaker_id"], to, seg["manual"], 1, 1 if e else 0,
           e["counted_for"] if e else None, e["weight"] if e else None)
    db.execute("UPDATE segments SET speaker_id=?, manual=1 WHERE id=?", (to, seg["id"]))
    if e:
        w, amb = manual_weight(db, seg["emb_id"], to)
        _move_emb(db, seg["emb_id"], to, w)
        db.execute("UPDATE embeddings SET ambiguous=? WHERE id=?", (1 if amb else 0, seg["emb_id"]))


def _tx(db, fn):
    db.execute("BEGIN IMMEDIATE")
    try:
        out = fn()
        dbm.bump_rev(db)
        db.execute("COMMIT")
        return out
    except BaseException:
        db.execute("ROLLBACK")
        raise


SEG_COLS = "id, ts, day, text, speaker_id, manual, emb_id, cluster_id, noise"


def reassign(db, seg_id, speaker_id=None, name=None, who="site", propagate=True):
    """Fred picks a person for one line. The line becomes manual, the voiceprints update, and (by default) the rest
    of that day is re-scored and propagated. Returns the batch ids and the real applied/flagged lists."""
    def run():
        seg = db.execute(f"SELECT {SEG_COLS} FROM segments WHERE id=?", (seg_id,)).fetchone()
        if not seg:
            raise LookupError("no such line")
        to = person(db, speaker_id, name)
        if seg["manual"] and seg["speaker_id"] is not None and resolve(db, seg["speaker_id"]) == to:
            # already set by hand to this person: a repeat click (2026-10-24, line 298 got 7 identical saves in 4 s
            # while the picker still showed the old name) must not add audit rows or run propagation again
            return {"batch_id": None, "speaker_id": to, "propagation": None, "unchanged": True}
        batch = new_batch()
        _manual_set(db, seg, to, batch, "reassign", who)
        prop = _propagate(db, seg, to, batch, who) if propagate else None
        return {"batch_id": batch, "speaker_id": to, "propagation": prop}
    return _tx(db, run)


def bulk(db, seg_ids, speaker_id=None, name=None, who="site"):
    """Reassign a selected range by hand in one undoable batch (no propagation: Fred chose every line)."""
    def run():
        to = person(db, speaker_id, name)
        batch = new_batch()
        n = 0
        for sid in sorted({int(i) for i in seg_ids}):
            seg = db.execute(f"SELECT {SEG_COLS} FROM segments WHERE id=?", (sid,)).fetchone()
            if seg:
                _manual_set(db, seg, to, batch, "bulk", who)
                n += 1
        return {"batch_id": batch, "speaker_id": to, "lines": n}
    return _tx(db, run)


def _propagate(db, src, to, parent, who):
    """Re-score every other line of src's day against the person's updated voiceprint and src's diarizer cluster.
    High confidence -> applied (manual stays 0, so a later hand choice still wins); low -> flagged as a suggestion.
    Lines set by hand are never touched. Future lines follow via cluster_map and the updated voiceprint."""
    batch = new_batch()
    c_to = centroid(db, to)
    cl = src["cluster_id"]
    cl_row = db.execute("SELECT named FROM speakers WHERE id=?", (cl,)).fetchone() if cl else None
    cluster_open = bool(cl and cl != to and cl_row is not None and not cl_row["named"])   # an unnamed voice
    applied, flagged = [], []
    if cluster_open:
        db.execute("INSERT INTO cluster_map(cluster_id,person_id,batch_id,active,created) VALUES(?,?,?,1,?) "
                   "ON CONFLICT(cluster_id) DO UPDATE SET person_id=excluded.person_id, batch_id=excluded.batch_id, "
                   "active=1, created=excluded.created", (cl, to, batch, time.time()))
        _audit(db, batch, "cluster_map", who, frm=cl, to=to, parent=parent, detail={"cluster": cl})
    rows = db.execute(f"""SELECT s.id, s.ts, s.text, s.speaker_id, s.cluster_id, s.emb_id, e.vec, sp.named AS cur_named
                          FROM segments s LEFT JOIN embeddings e ON e.id=s.emb_id LEFT JOIN speakers sp ON sp.id=s.speaker_id
                          WHERE s.day=? AND s.id<>? AND s.manual=0 AND s.noise=0
                            AND (s.speaker_id IS NULL OR s.speaker_id<>?) ORDER BY s.ts""",
                      (src["day"], src["id"], to)).fetchall()
    cents = {}
    for r in rows:
        same_cluster = cluster_open and r["cluster_id"] == cl
        if r["vec"] is None and not same_cluster:
            continue
        e = dbm.blob_to_vec(r["vec"]) if r["vec"] is not None else None
        s = float(np.dot(e, c_to)) if (e is not None and c_to is not None) else None
        cur = None
        if e is not None and r["speaker_id"] is not None and r["cur_named"]:
            if r["speaker_id"] not in cents:
                cents[r["speaker_id"]] = centroid(db, r["speaker_id"])
            if cents[r["speaker_id"]] is not None:
                cur = float(np.dot(e, cents[r["speaker_id"]]))
        beats_current = cur is None or (s is not None and s >= cur + config.PROP_MARGIN)
        auto = beats_current and s is not None and (s >= config.PROP_HIGH or (same_cluster and s >= config.PROP_CLUSTER))
        item = {"id": r["id"], "ts": r["ts"], "text": r["text"], "score": None if s is None else round(s, 3),
                "same_cluster": bool(same_cluster), "from_speaker": r["speaker_id"]}
        if auto:
            _audit(db, batch, "propagate", who, r["id"], r["speaker_id"], to, 0, 0, 0, score=s, parent=parent,
                   detail={"same_cluster": bool(same_cluster)})
            db.execute("UPDATE segments SET speaker_id=? WHERE id=? AND manual=0", (to, r["id"]))
            applied.append(item)
        elif beats_current and ((s is not None and s >= config.PROP_LOW) or same_cluster):
            item["suggestion_id"] = db.execute(
                "INSERT INTO suggestions(batch_id,segment_id,person_id,score,same_cluster,created) VALUES(?,?,?,?,?,?)",
                (batch, r["id"], to, s if s is not None else 0.0, 1 if same_cluster else 0, time.time())).lastrowid
            flagged.append(item)
    return {"batch_id": batch, "applied": applied, "flagged": flagged, "cluster_mapped": cl if cluster_open else None,
            "thresholds": {"high": config.PROP_HIGH, "low": config.PROP_LOW, "cluster": config.PROP_CLUSTER}}


def decide_suggestion(db, sug_id, accept, who="site"):
    def run():
        g = db.execute("SELECT * FROM suggestions WHERE id=?", (sug_id,)).fetchone()
        if not g:
            raise LookupError("no such suggestion")
        if g["state"] != "open":
            return {"state": g["state"]}
        out = {"state": "accepted" if accept else "rejected"}
        if accept:
            seg = db.execute(f"SELECT {SEG_COLS} FROM segments WHERE id=?", (g["segment_id"],)).fetchone()
            batch = new_batch()
            _manual_set(db, seg, resolve(db, g["person_id"]), batch, "accept", who)
            out["batch_id"] = batch
        db.execute("UPDATE suggestions SET state=?, decided=? WHERE id=?", (out["state"], time.time(), sug_id))
        return out
    return _tx(db, run)


def rename(db, sid, name, who="site"):
    """Rename a voice everywhere (one row, so every line follows). A name that already exists merges into it."""
    def run():
        me = db.execute("SELECT id, name, named FROM speakers WHERE id=? AND merged_into IS NULL", (sid,)).fetchone()
        if not me:
            raise LookupError("no such speaker")
        clean = " ".join(str(name).split())[:60]
        if not clean:
            raise ValueError("name required")
        other = db.execute("SELECT id FROM speakers WHERE name=? COLLATE NOCASE AND id<>? AND merged_into IS NULL",
                           (clean, sid)).fetchone()
        batch = new_batch()
        if other:
            _merge(db, sid, other["id"], batch, who)
            db.execute("UPDATE speakers SET named=1 WHERE id=?", (other["id"],))
            return {"batch_id": batch, "speaker_id": other["id"], "merged": True}
        _audit(db, batch, "rename", who, frm=sid, to=sid, detail={"old": me["name"], "old_named": me["named"], "new": clean})
        db.execute("UPDATE speakers SET name=?, named=1 WHERE id=?", (clean, sid))
        return {"batch_id": batch, "speaker_id": sid, "merged": False}
    return _tx(db, run)


def _merge(db, src, dst, batch, who):
    """Fold voice src into dst. Lines are re-pointed (audited one by one), never deleted; src stays as a row with
    merged_into so the merge can be undone."""
    segs = [r["id"] for r in db.execute("SELECT id FROM segments WHERE speaker_id=?", (src,))]
    for s in segs:
        _audit(db, batch, "merge", who, s, src, dst)
    db.execute("UPDATE segments SET speaker_id=? WHERE speaker_id=?", (dst, src))
    embs = [r["id"] for r in db.execute("SELECT id FROM embeddings WHERE counted_for=?", (src,))]
    db.execute("UPDATE embeddings SET counted_for=? WHERE counted_for=?", (dst, src))
    _audit(db, batch, "merge_voice", who, frm=src, to=dst, detail={"src": src, "dst": dst, "embeddings": embs})
    db.execute("UPDATE speakers SET merged_into=? WHERE id=?", (dst, src))
    dbm.recompute_voiceprint(db, dst)
    dbm.recompute_voiceprint(db, src)


def set_color(db, sid, color, who="site"):
    c = str(color or "").strip().lower()
    if c and not (len(c) == 7 and c[0] == "#" and all(x in "0123456789abcdef" for x in c[1:])):
        raise ValueError("color must be #rrggbb")
    if c and contrast(c) < 4.5:
        raise ValueError(f"contrast {contrast(c):.1f}:1 on the dark background is too low (need 4.5:1)")

    def run():
        r = db.execute("SELECT color FROM speakers WHERE id=?", (sid,)).fetchone()
        if not r:
            raise LookupError("no such speaker")
        batch = new_batch()
        _audit(db, batch, "color", who, frm=sid, to=sid, detail={"old": r["color"], "new": c or None})
        db.execute("UPDATE speakers SET color=? WHERE id=?", (c or None, sid))
        return {"batch_id": batch}
    return _tx(db, run)


def confirm(db, sid, who="site"):
    """'Confirm this is me'. Only ever called from Fred's click on the site."""
    def run():
        r = db.execute("SELECT confirmed FROM speakers WHERE id=?", (sid,)).fetchone()
        if not r:
            raise LookupError("no such speaker")
        batch = new_batch()
        _audit(db, batch, "confirm", who, frm=sid, to=sid, detail={"old": r["confirmed"]})
        db.execute("UPDATE speakers SET confirmed=1 WHERE id=?", (sid,))
        return {"batch_id": batch}
    return _tx(db, run)


def undo(db, batch_id, who="site"):
    """Revert one batch (and any propagation batch it caused). A line is only reverted if it still carries the
    speaker this batch gave it, so later edits are never clobbered. Returns counts."""
    def run():
        return _undo(db, batch_id, who)
    return _tx(db, run)


def _undo(db, batch_id, who):
    out = {"reverted": 0, "skipped": 0}
    for ch in [r[0] for r in db.execute("SELECT DISTINCT batch_id FROM audit WHERE parent_batch=? AND undone_at IS NULL", (batch_id,))]:
        sub = _undo(db, ch, who)
        out["reverted"] += sub["reverted"]
        out["skipped"] += sub["skipped"]
    rows = db.execute("SELECT * FROM audit WHERE batch_id=? AND undone_at IS NULL ORDER BY id DESC", (batch_id,)).fetchall()
    if not rows:
        return out
    now = time.time()
    touched = set()
    for a in rows:
        k = a["kind"]
        if k in ("reassign", "bulk", "accept", "propagate", "merge"):
            cur = db.execute("SELECT speaker_id, emb_id FROM segments WHERE id=?", (a["segment_id"],)).fetchone()
            if cur and cur["speaker_id"] == a["to_speaker"]:
                if k == "merge":
                    db.execute("UPDATE segments SET speaker_id=? WHERE id=?", (a["from_speaker"], a["segment_id"]))
                else:
                    db.execute("UPDATE segments SET speaker_id=?, manual=? WHERE id=?",
                               (a["from_speaker"], a["from_manual"], a["segment_id"]))
                if a["vp_moved"] and cur["emb_id"]:
                    _move_emb(db, cur["emb_id"], a["emb_counted_before"], a["emb_weight_before"] or 1.0)
                out["reverted"] += 1
            else:
                out["skipped"] += 1
        elif k == "merge_voice":
            d = json.loads(a["detail"])
            if d["embeddings"]:
                ph = ",".join("?" * len(d["embeddings"]))
                db.execute(f"UPDATE embeddings SET counted_for=? WHERE id IN ({ph}) AND counted_for=?",
                           [d["src"], *d["embeddings"], d["dst"]])
            db.execute("UPDATE speakers SET merged_into=NULL WHERE id=?", (d["src"],))
            touched.update((d["src"], d["dst"]))
        elif k == "cluster_map":
            db.execute("UPDATE cluster_map SET active=0 WHERE cluster_id=? AND batch_id=?", (a["from_speaker"], batch_id))
        elif k == "rename":
            d = json.loads(a["detail"])
            db.execute("UPDATE speakers SET name=?, named=? WHERE id=?", (d["old"], d["old_named"], a["from_speaker"]))
        elif k == "color":
            db.execute("UPDATE speakers SET color=? WHERE id=?", (json.loads(a["detail"])["old"], a["from_speaker"]))
        elif k == "confirm":
            db.execute("UPDATE speakers SET confirmed=? WHERE id=?", (json.loads(a["detail"])["old"], a["from_speaker"]))
    for sid in touched:
        dbm.recompute_voiceprint(db, sid)
    db.execute("UPDATE suggestions SET state='void', decided=? WHERE batch_id=? AND state='open'", (now, batch_id))
    db.execute("UPDATE audit SET undone_at=? WHERE batch_id=? AND undone_at IS NULL", (now, batch_id))
    _audit(db, new_batch(), "undo", who, detail={"batch": batch_id, **out})
    return out


def history(db, limit=40):
    rows = db.execute("""SELECT batch_id, MIN(kind) AS kind, GROUP_CONCAT(DISTINCT kind) AS kinds, COUNT(*) AS n,
                                MIN(created) AS created, MAX(undone_at) AS undone_at, MAX(who) AS who,
                                MAX(parent_batch) AS parent, MAX(to_speaker) AS to_speaker
                         FROM audit WHERE kind<>'undo' GROUP BY batch_id ORDER BY MIN(id) DESC LIMIT ?""", (limit,)).fetchall()
    out = []
    for r in rows:
        nm = db.execute("SELECT name FROM speakers WHERE id=?", (r["to_speaker"],)).fetchone() if r["to_speaker"] else None
        out.append({"batch_id": r["batch_id"], "kinds": r["kinds"].split(","), "lines": r["n"], "created": r["created"],
                    "undone": r["undone_at"] is not None, "who": r["who"], "parent": r["parent"], "to": nm[0] if nm else None})
    return out


def suggestions(db):
    """'Speaker N sounds like X: same person?' for unnamed voices with >= MERGE_MIN_LINES lines whose voiceprint is
    closest to one named person by >= MERGE_MARGIN (and at least MERGE_FLOOR). Fred answers with one click: yes is
    the existing merge (rename to that name), no is remembered. Nothing is merged automatically."""
    import json
    dismissed = set(json.loads(dbm.get_meta(db, "merge_dismissed", "[]") or "[]"))
    named = [(r["id"], r["name"], dbm.blob_to_vec(r["vec_sum"])) for r in db.execute(
        """SELECT s.id, s.name, v.vec_sum FROM speakers s JOIN voiceprints v ON v.speaker_id=s.id
           WHERE s.named=1 AND s.merged_into IS NULL AND v.n>0""")]
    named = [(i, n, v / np.linalg.norm(v)) for i, n, v in named if np.linalg.norm(v) > 0]
    out = []
    if not named:
        return out
    for r in db.execute("""SELECT s.id, s.name, v.vec_sum, v.n, (SELECT COUNT(*) FROM segments g WHERE g.speaker_id=s.id AND g.noise=0) lines
                           FROM speakers s JOIN voiceprints v ON v.speaker_id=s.id
                           WHERE s.named=0 AND s.merged_into IS NULL AND v.n>0"""):
        if r["lines"] < config.MERGE_MIN_LINES:
            continue
        c = dbm.blob_to_vec(r["vec_sum"])
        if np.linalg.norm(c) == 0:
            continue
        c = c / np.linalg.norm(c)
        sims = sorted(((float(c @ v), i, n) for i, n, v in named), reverse=True)
        best, pid, pname = sims[0]
        second = sims[1][0] if len(sims) > 1 else -1.0
        if best < config.MERGE_FLOOR or best - second < config.MERGE_MARGIN or f"{r['id']}:{pid}" in dismissed:
            continue
        out.append({"cluster_id": r["id"], "cluster": r["name"], "lines": r["lines"], "samples": r["n"],
                    "person_id": pid, "person": pname, "sim": round(best, 3),
                    "next": ({"person": sims[1][2], "sim": round(second, 3)} if len(sims) > 1 else None)})
    return sorted(out, key=lambda x: -x["lines"])


def dismiss_suggestion(db, cluster_id, person_id):
    import json
    cur = set(json.loads(dbm.get_meta(db, "merge_dismissed", "[]") or "[]"))
    cur.add(f"{int(cluster_id)}:{int(person_id)}")
    dbm.set_meta(db, "merge_dismissed", json.dumps(sorted(cur)))
    return {"dismissed": len(cur)}
