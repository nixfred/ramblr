"""Media detection (queue item B2-1): is a line the room talking, or a TV / music / movie playing?

1. Window classifier. YAMNet (AudioSet, 521 classes; Apache-2.0 ONNX export, raw 16 kHz waveform in) runs on
   the Intel NPU through OpenVINO beside Whisper, one fixed 2.88 s window at a time (~4 ms per window). If the
   NPU cannot take it, it runs on the CPU in a thread at nice 19. Never the GPU. Only scores are stored
   (table media_windows); no audio is kept. Quiet windows are not stored at all.
2. Line decision. A small logistic model over features of the windows a line overlaps (music, TV/radio,
   crowd/laughter, narration) plus who spoke: a known, named voice over the TV is kept as talk (talk-over); an
   unnamed voice that keeps turning up while media plays is a media hint (a steady voice matching nobody).
3. Corrections. "That was the TV" / "not media" on a line or range sets a manual flag (never overridden) and is
   a training example: the weights are refit on every correction, pulled toward the hand-set prior.

segments.media: NULL = not decided yet, 0 = talk, 1 = media (auto), 2 = media (marked by hand), -1 = talk (marked
by hand). Lines are never deleted; media lines keep their text and are only left out of the transcript view,
idea detection, action items, summaries and Ask."""
import json
import logging
import os
import queue
import threading
import time
import uuid

import numpy as np

from . import config, db as dbm, notify

log = logging.getLogger("ramblr.media")

WINDOW = 46080                      # samples: 2.88 s = YAMNet's 0.96 s patch + 4 hops of 0.48 s (5 frames)
GROUPS = {
    "music": list(range(132, 277)) + list(range(24, 32)),   # Music and all its genres/instruments, plus singing
    "tv": [518, 519, 5],                                     # Television, Radio, Speech synthesizer
    "crowd": [13, 15, 16, 17, 61, 62, 63, 64, 65],           # laughter, cheering, applause, crowd, babble
    "narration": [3],
    "speech": [0, 1, 2],
    "silence": [494],
}
FEATURES = ["music", "tv", "crowd", "narration", "known", "unknown_media_ratio", "unknown", "cast", "occ"]
PRIOR = {"bias": -2.2, "music": 5.0, "tv": 6.0, "crowd": 3.0, "narration": 1.0, "known": -5.0,
         "unknown_media_ratio": 2.5, "unknown": 0.6,
         "cast": 1.5,   # B2-2: a Cast device (the TV) was playing; alone it never tags a line (-2.2 + 1.5 < 0)
         "occ": 3.5}    # speech occupancy ramp: broadcasts never pause (vic video 0.82-1.0, room news 0.7-0.93), people
                        # do (family talk max 0.59, Fred's vic lines 0.01-0.06). Fred 8:06 AM: detect TV on its own.
OCC_WINDOW = 120.0     # seconds of the same source looked back over for occupancy
L2 = 8.0            # pull toward the prior, worth about 8 examples: one click nudges, many consistent clicks steer


# ---- classifier (capture process) ----
class Classifier:
    def __init__(self, device=None):
        import openvino as ov
        core = ov.Core()
        path = str(config.MEDIA_MODEL)
        cache = config.DATA / "cache" / "yamnet"
        cache.mkdir(parents=True, exist_ok=True)
        self.device = None
        for dev in ([device] if device else ["NPU", "CPU"]):
            try:
                m = core.read_model(path)
                m.reshape({m.inputs[0].get_any_name(): [WINDOW]})
                self.model = core.compile_model(m, dev, {"CACHE_DIR": str(cache)} if dev == "NPU" else {"INFERENCE_NUM_THREADS": 1})
                self.device = dev
                break
            except Exception as e:
                log.warning("YAMNet on %s failed: %s", dev, str(e)[:200])
        if not self.device:
            raise RuntimeError("YAMNet could not be compiled on the NPU or the CPU")
        self.out = self.model.outputs[0]

    def scores(self, samples):
        """Mean class scores (521) over the window's frames."""
        return self.model(np.ascontiguousarray(samples, dtype=np.float32))[self.out].mean(0)


def group_scores(s):
    out = {k: float(np.max(s[idx])) for k, idx in GROUPS.items()}
    out["top"] = int(np.argmax(s))
    return out


class Worker:
    """Takes PCM from the capture loop, classifies fixed windows in its own thread, stores the scores and tags
    newly finished lines. feed() never blocks the capture loop: if the worker falls behind, audio is dropped."""

    def __init__(self, db_path, source=None):
        self.db_path = db_path
        self.source = source       # None = the room mic; "mic:<device>" = a remote mic with its own windows
        self.q = queue.Queue(maxsize=300)        # ~30 s of 100 ms chunks
        self.buf = np.zeros(0, np.float32)
        self.buf_end = None
        self.ok = True
        self.device = None
        self.music = []            # consecutive music windows held in RAM for one fingerprint (never written to disk)
        self.music_t0 = None
        self.t = threading.Thread(target=self._run, daemon=True, name="media")
        self.t.start()

    def feed(self, samples, end_ts):
        try:
            self.q.put_nowait((samples, end_ts))
        except queue.Full:
            pass

    def _run(self):
        try:
            clf = Classifier()
        except Exception:
            log.exception("media classifier unavailable; lines will not be tagged")
            self.ok = False
            return
        self.device = clf.device
        if clf.device == "CPU":
            try:
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)   # this thread only
            except OSError:
                pass
        log.warning("media classifier YAMNet ready on %s", clf.device)
        db = dbm.connect(self.db_path)
        dbm.set_meta(db, "media_device", clf.device)
        while True:
            samples, end_ts = self.q.get()
            if self.buf_end is not None and end_ts - len(samples) / config.RATE - self.buf_end > 1.0:
                self.buf = np.zeros(0, np.float32)     # a gap (mute, mic switch): never glue audio across it
            self.buf = np.concatenate([self.buf, samples])
            self.buf_end = end_ts
            while len(self.buf) >= WINDOW:
                win, self.buf = self.buf[:WINDOW], self.buf[WINDOW:]
                t1 = self.buf_end - len(self.buf) / config.RATE
                t0 = t1 - WINDOW / config.RATE
                try:
                    self._window(db, clf, win, t0, t1)
                except Exception:
                    log.exception("media window failed")

    def _window(self, db, clf, win, t0, t1):
        rms = float(np.sqrt(np.mean(win * win)))
        if self.source is None:
            dbm.set_meta(db, "mic_rms", round(rms, 5))      # a level number for the health page, never audio
            dbm.set_meta(db, "mic_rms_at", round(t1, 1))
        g = None
        if rms >= config.MEDIA_MIN_RMS:
            g = group_scores(clf.scores(win))
            if g["silence"] < 0.8:
                db.execute("""INSERT INTO media_windows(ts0,ts1,music,tv,crowd,narration,speech,top,rms,source)
                              VALUES(?,?,?,?,?,?,?,?,?,?)""",
                           (t0, t1, g["music"], g["tv"], g["crowd"], g["narration"], g["speech"], g["top"], rms, self.source))
        self._music(db, win, t0, t1, g if rms >= config.MEDIA_MIN_RMS else None)
        changed = tag_pending(db, until=t1, source=self.source)
        if changed:
            notify.send({"t": "media", "ids": changed})


    def _music(self, db, win, t0, t1, g):
        """Hold up to ~20 s of a song in RAM, fingerprint it (fpcalc, if installed), drop the audio."""
        from . import musicid
        playing = bool(g) and g["music"] >= config.MUSIC_MIN
        if playing and musicid.fpcalc():
            if not self.music:
                self.music_t0 = t0
            self.music.append(win)
        if self.music and (not playing or len(self.music) * WINDOW >= 20 * config.RATE):
            clip, ts0 = np.concatenate(self.music), self.music_t0
            self.music, self.music_t0 = [], None
            res = musicid.fingerprint(clip)
            del clip
            if res:
                db.execute("INSERT INTO media_tracks(ts0,ts1,fingerprint,duration,created,source) VALUES(?,?,?,?,?,?)",
                           (ts0, t1, res[0], res[1], time.time(), self.source))


def sessions(db, a, b, gap=60, min_len=20, source=None):
    """Media periods from one mic's classifier windows: [{t0, t1, kind: music|tv}], merged across short gaps. A window
    is music only if music is strong and speech is weak; narrated media (speech + music bed) is tv."""
    out = []
    from . import calls
    for r in db.execute("""SELECT ts0, ts1, music, tv, narration, speech FROM media_windows WHERE ts1>=? AND ts0<? AND source IS ?
                           AND (music>=0.5 OR tv>=0.4) ORDER BY ts0""", (a, b, source)):
        if source is None and calls.during(db, r["ts0"], r["ts1"]):
            continue                                        # a caller out of the speakers, not media
        kind = "music" if r["music"] >= MUSIC_MIN and (r["speech"] or 0) < MUSIC_MAX_SPEECH else "tv"
        if out and r["ts0"] - out[-1]["t1"] < gap:
            out[-1]["t1"] = r["ts1"]
            out[-1]["k"][kind] = out[-1]["k"].get(kind, 0) + 1
        else:
            out.append({"t0": r["ts0"], "t1": r["ts1"], "k": {kind: 1}})
    res = []
    for s in out:
        if s["t1"] - s["t0"] >= min_len:
            res.append({"t0": s["t0"], "t1": s["t1"], "kind": max(s["k"], key=s["k"].get)})
    return res


# ---- decision (any process; pure DB + numpy) ----
def load_model(db):
    m = json.loads(dbm.get_meta(db, "media_model", "null") or "null")
    return m or {"w": dict(PRIOR), "n": 0}


def features(db, seg):
    """Feature dict for one line (sqlite Row with id, ts, dur, speaker_id, cluster_id)."""
    a, b = seg["ts"], seg["ts"] + max(seg["dur"], 0.3)
    src = seg["source"] if "source" in seg.keys() else None      # each mic is judged only by its own windows
    ws = db.execute("SELECT ts0, ts1, music, tv, crowd, narration FROM media_windows WHERE ts1>? AND ts0<? AND source IS ?", (a, b, src)).fetchall()
    if not ws:   # a short line between stored windows: borrow the nearest window within 1.5 s
        ws = db.execute("""SELECT ts0, ts1, music, tv, crowd, narration FROM media_windows WHERE ts1>? AND ts0<? AND source IS ?
                           ORDER BY ABS((ts0+ts1)/2 - ?) LIMIT 1""", (a - 1.5, b + 1.5, src, (a + b) / 2)).fetchall()
    f = {"music": 0.0, "tv": 0.0, "crowd": 0.0, "narration": 0.0, "known": 0.0, "unknown_media_ratio": 0.0, "unknown": 0.0, "cast": 0.0, "occ": 0.0}
    busy = db.execute("""SELECT SUM(MIN(ts+dur, ?) - MAX(ts, ?)) FROM segments WHERE source IS ? AND ts<? AND ts+dur>?""",
                      (b, a - OCC_WINDOW, src, b, a - OCC_WINDOW)).fetchone()[0] or 0.0
    f["occ"] = round(min(max((busy / (OCC_WINDOW + (b - a)) - 0.5) / 0.4, 0.0), 1.0), 4)
    from . import players
    if src is None and players.cast_playing(db, a, b):     # the Cast TV is in the living room, heard by the room mic
        f["cast"] = 1.0
    tot = 0.0
    for w in ws:        # mean over the windows the line overlaps, weighted by how much of the line each covers
        ov = max(min(w["ts1"], b) - max(w["ts0"], a), 0.05)
        tot += ov
        for k in ("music", "tv", "crowd", "narration"):
            f[k] += ov * (w[k] or 0.0)
    if tot:
        for k in ("music", "tv", "crowd", "narration"):
            f[k] = round(f[k] / tot, 4)
    sp = None
    if seg["speaker_id"]:
        sp = db.execute("SELECT named, confirmed FROM speakers WHERE id=?", (seg["speaker_id"],)).fetchone()
    if sp and sp["named"]:
        f["known"] = 1.0                       # Fred or another known voice: talking over the TV is still talk
    elif seg["speaker_id"] or seg["cluster_id"]:
        f["unknown"] = 1.0
        c = seg["cluster_id"] or seg["speaker_id"]
        # Fred's own labels only (2 = TV, -1 = talk), same mic only: counting auto tags let a room-average cluster
        # feed on its own mistakes (2026-10-24: 27 of 40 family-talk lines hidden by this feature alone)
        r = db.execute("""SELECT COUNT(*) n, SUM(CASE WHEN media=2 THEN 1 ELSE 0 END) m FROM segments
                          WHERE (cluster_id=? OR speaker_id=?) AND source IS ? AND media IN (2,-1) AND id<>?
                          AND id IN (SELECT segment_id FROM media_corrections)""", (c, c, src, seg["id"])).fetchone()   # clicks, not spreads
        if r["n"] >= 5:
            f["unknown_media_ratio"] = (r["m"] or 0) / r["n"]   # a voice Fred has mostly marked as TV
    return f


def prob(model, f):
    w = model["w"]
    z = w["bias"] + sum(w.get(k, PRIOR[k]) * f.get(k, 0.0) for k in FEATURES)   # older models/corrections lack new features
    return 1.0 / (1.0 + np.exp(-z))


SEG_COLS = "id, ts, dur, speaker_id, cluster_id, media, noise, source"
STICKY_GAP = 60.0      # a media stretch on one source carries on to its next line within this gap ...
STICKY_MIN_P = 0.3     # ... if that line looks at least a bit like media itself (Fred E2: "media_p over 0.3")


def sticky(db, seg, f):
    """The same source was playing media just before this line (auto or Fred's tag) and the line is not a confident
    known voice: the TV is still on. Fred 8:03 AM: "train it to follow the TV conversation and mark it TV"."""
    if f.get("known"):
        return False
    prev = db.execute("""SELECT ts, dur, media FROM segments WHERE source IS ? AND ts<? AND id<>? AND media IS NOT NULL AND noise=0
                         ORDER BY ts DESC LIMIT 1""", (seg["source"] if "source" in seg.keys() else None, seg["ts"], seg["id"])).fetchone()
    return bool(prev and prev["media"] in (1, 2) and seg["ts"] - (prev["ts"] + prev["dur"]) <= STICKY_GAP)


def talk_carry(db, seg):
    """Fred's latest label in this unbroken stretch of the same source (gaps <= STICKY_GAP) was "not media" (-1):
    the stretch is still talk, so no automatic TV. A later "that was the TV" (2) ends it. 2026-10-24: Fred marked
    a Zoom caller's lines not-TV four times and each next line came back TV (p 0.89-0.93)."""
    edge = seg["ts"]
    for r in db.execute("""SELECT ts, dur, media FROM segments WHERE source IS ? AND ts<? AND id<>? AND noise=0
                           ORDER BY ts DESC LIMIT 400""", (seg["source"] if "source" in seg.keys() else None, seg["ts"], seg["id"])):
        if edge - (r["ts"] + r["dur"]) > STICKY_GAP:
            return False
        if r["media"] == -1:
            return True
        if r["media"] == 2:
            return False
        edge = r["ts"]
    return False


def in_call(db, seg):
    """Room-mic line spoken while a call was live (calls.py): a caller out of the speakers is a person, not TV."""
    if ("source" in seg.keys() and seg["source"] is not None):
        return False
    from . import calls
    return calls.during(db, seg["ts"], seg["ts"] + max(seg["dur"], 0.3))


def decide(db, seg, model=None):
    model = model or load_model(db)
    f = features(db, seg)
    p = float(prob(model, f))
    m = 1 if p >= 0.5 else 0
    if m and (in_call(db, seg) or talk_carry(db, seg)):
        m = 0                                                  # call mode, or Fred already said this stretch is talk
    elif not m and p >= STICKY_MIN_P and not in_call(db, seg) and sticky(db, seg, f):   # follow the stretch, never over talk
        m = 1
    return m, p, f


def tag_pending(db, until, source=None):
    """Decide every undecided line that ended before `until` (the classifier has heard past it)."""
    model = load_model(db)
    changed = []
    # uploads and imports have no classifier and never heard the room's TV: they are talk. Each mic (the room,
    # mic:<device>) is tagged only by its own classifier, from its own windows.
    if source is None:
        db.execute("UPDATE segments SET media=0 WHERE media IS NULL AND source IS NOT NULL AND source NOT LIKE 'mic:%' AND ts+dur<=?", (until,))
    for s in db.execute(f"SELECT {SEG_COLS} FROM segments WHERE media IS NULL AND source IS ? AND ts+dur<=? ORDER BY ts LIMIT 200", (source, until)).fetchall():
        m, p, _ = decide(db, s, model)
        if s["noise"]:
            m = 0
        db.execute("UPDATE segments SET media=?, media_p=? WHERE id=? AND media IS NULL", (m, round(p, 3), s["id"]))
        if m:
            changed.append(s["id"])
    return changed


def retag(db, ids):
    """Re-decide auto-tagged lines (after diarization names the speaker). Manual flags are never touched."""
    if not ids:
        return []
    model = load_model(db)
    ph = ",".join("?" * len(ids))
    changed = []
    for s in db.execute(f"SELECT {SEG_COLS} FROM segments WHERE id IN ({ph}) AND media IN (0,1) AND (source IS NULL OR source LIKE 'mic:%') ORDER BY ts",
                        list(ids)).fetchall():
        m, p, _ = decide(db, s, model)
        if s["noise"]:
            m = 0
        if m != s["media"]:   # only if still the automatic value we read: a manual flag set meanwhile always wins
            if db.execute("UPDATE segments SET media=?, media_p=? WHERE id=? AND media=?", (m, round(p, 3), s["id"], s["media"])).rowcount:
                changed.append(s["id"])
    return changed


def correct(db, ids, is_media, who="site"):
    """Fred's one-click correction for lines. Records each as a training example and refits the weights."""
    ids = sorted({int(i) for i in ids})
    if not ids:
        raise ValueError("no lines")
    ph = ",".join("?" * len(ids))
    rows = db.execute(f"SELECT {SEG_COLS} FROM segments WHERE id IN ({ph})", ids).fetchall()
    if len(rows) != len(ids):
        raise LookupError("unknown line id")
    now = time.time()
    for s in rows:
        f = dict(features(db, s), src=s["source"] or "room")     # which mic these acoustic numbers came from
        db.execute("INSERT INTO media_corrections(segment_id, label, features, who, created) VALUES(?,?,?,?,?)",
                   (s["id"], 1 if is_media else 0, json.dumps(f), who, now))
        db.execute("UPDATE segments SET media=? WHERE id=?", (2 if is_media else -1, s["id"]))
    model = fit(db)
    # the new weights re-decide that day's other auto lines, so one correction teaches the rest of the day
    days = sorted({r["day"] for r in db.execute(f"SELECT DISTINCT day FROM segments WHERE id IN ({ph})", ids)})
    others = [r[0] for r in db.execute(f"SELECT id FROM segments WHERE day IN ({','.join('?' * len(days))}) AND media IN (0,1)", days)]
    changed = retag(db, others)
    out = {"lines": len(ids), "media": bool(is_media), "retagged": changed, "corrections": model["n"], "spread": [], "batch": None}
    if is_media:                 # E2b (Fred 8:00 AM): "once I label TV can it follow the rest of that to mark it tv?"
        out["batch"] = uuid.uuid4().hex[:12]
        out["spread"] = spread(db, ids, out["batch"], who)
        if not out["spread"]:
            out["batch"] = None
    return out


SPREAD_GAP = 60.0      # a pause longer than this ends the stretch
SPREAD_FLOOR = 0.2     # a neighbour whose own media_p is below this is talk, not the same TV
SPREAD_SIMILAR = 0.4   # ... or one this far from the marked line's media_p


def spread(db, seed_ids, batch, who="site"):
    """Fred marked lines as TV: walk each one's mic backwards and forwards in time over automatic lines that still
    look like the same source, and mark them too, as one undoable batch (audit kind media_spread). Stops at a gap
    over SPREAD_GAP, a line that no longer looks similar, a voice change on a line that does not itself look like
    media, a confident known voice, or any line Fred decided himself (never changed). While the Cast TV is playing
    (room mic) a voice change does not stop the walk: the TV has many voices. Never deletes anything."""
    from . import players
    model = load_model(db)
    marked, done = [], set(int(i) for i in seed_ids)
    now = time.time()
    for seed in db.execute(f"SELECT {SEG_COLS} FROM segments WHERE id IN ({','.join('?' * len(done))}) ORDER BY ts", list(done)).fetchall():
        sp = float(prob(model, features(db, seed)))
        for forward in (False, True):
            prev = seed
            while True:
                q = db.execute(f"""SELECT {SEG_COLS}, manual FROM segments WHERE source IS ? AND noise=0 AND id<>? AND
                                   {"ts>? ORDER BY ts" if forward else "ts<? ORDER BY ts DESC"} LIMIT 1""",
                               (prev["source"], prev["id"], prev["ts"])).fetchone()
                if not q:
                    break
                gap = q["ts"] - (prev["ts"] + prev["dur"]) if forward else prev["ts"] - (q["ts"] + q["dur"])
                if gap > SPREAD_GAP or q["media"] in (2, -1) and q["id"] not in done or q["manual"]:
                    break                                   # a pause, or Fred's own decision: a wall
                f = features(db, q)
                p = float(prob(model, f))
                if f["known"]:
                    break                                   # a confident known voice: a person, not the TV
                cast = q["source"] is None and players.cast_playing(db, q["ts"], q["ts"] + q["dur"])
                if not cast:
                    if p < SPREAD_FLOOR or abs(p - sp) > SPREAD_SIMILAR:
                        break                               # no longer looks like the same source
                    if q["cluster_id"] and prev["cluster_id"] and q["cluster_id"] != prev["cluster_id"] and p < 0.5:
                        break                               # a different voice that does not look like media itself
                if q["id"] not in done and q["media"] != 2:
                    if db.execute("UPDATE segments SET media=2, media_p=? WHERE id=? AND media IS ?", (round(p, 3), q["id"], q["media"])).rowcount:
                        db.execute("""INSERT INTO audit(batch_id, kind, segment_id, detail, who, created)
                                      VALUES(?, 'media_spread', ?, ?, ?, ?)""", (batch, q["id"], json.dumps({"old": q["media"], "new": 2}), who, now))
                        marked.append(q["id"])
                done.add(q["id"])
                prev = q
    return marked


def undo_spread(db, batch):
    """Put every line of one spread batch back exactly as it was (only if nobody changed it since). Returns the ids."""
    back = []
    for a in db.execute("SELECT id, segment_id, detail FROM audit WHERE batch_id=? AND kind='media_spread' AND undone_at IS NULL", (batch,)).fetchall():
        old = json.loads(a["detail"])["old"]
        if db.execute("UPDATE segments SET media=? WHERE id=? AND media=2", (old, a["segment_id"])).rowcount:
            back.append(a["segment_id"])
        db.execute("UPDATE audit SET undone_at=? WHERE id=?", (time.time(), a["id"]))
    return back


def fit(db):
    """Logistic regression on all corrections, L2-regularised toward the hand-set prior (few examples cannot
    swing it wildly). Plain numpy gradient descent: a few hundred examples at most."""
    # the latest correction per line wins (Fred flipping a line back must not leave both labels fighting)
    rows = db.execute("""SELECT c.segment_id, c.label, c.features FROM media_corrections c
                         WHERE c.id = (SELECT MAX(id) FROM media_corrections WHERE segment_id=c.segment_id)""").fetchall()
    keys = ["bias"] + FEATURES
    prior = np.array([PRIOR[k] for k in keys])
    w = prior.copy()
    if rows:
        # features are recomputed from the current DB (per-source windows, new features like occ), so every past
        # label teaches the current model; the stored snapshot is only the fallback for a line that is gone
        def fx(r):
            seg = db.execute(f"SELECT {SEG_COLS} FROM segments WHERE id=?", (r["segment_id"],)).fetchone()
            return features(db, seg) if seg else json.loads(r["features"])
        X = np.array([[1.0] + [fx(r).get(k, 0.0) for k in FEATURES] for r in rows])
        y = np.array([r["label"] for r in rows], dtype=float)
        lr = 1.0 / (len(y) + L2)
        for _ in range(600):      # MAP: sum of log-losses + L2/2 * |w - prior|^2, so one click nudges, twenty steer
            p = 1 / (1 + np.exp(-(X @ w)))
            w -= lr * (X.T @ (p - y) + L2 * (w - prior))
    model = {"w": {k: round(float(v), 4) for k, v in zip(keys, w)}, "n": len(rows), "fitted": time.time()}
    dbm.set_meta(db, "media_model", json.dumps(model))
    return model


def blocks(rows):
    """Group consecutive media lines of a listing into blocks for the gray 'TV playing 8:10 to 9:40 PM' bar."""
    out, cur = [], None
    for r in rows:
        if r.get("media"):
            if cur and r["ts"] - cur["end"] < 600:
                cur["ids"].append(r["id"]); cur["end"] = r["ts"] + r["dur"]
            else:
                cur = {"ids": [r["id"]], "start": r["ts"], "end": r["ts"] + r["dur"]}
                out.append(cur)
        elif cur and not r.get("noise"):
            cur = None
    return out


MUSIC_MIN, MUSIC_MAX_SPEECH = 0.5, 0.3    # "truly music": music strong AND little speech (a narrated video is not music)


def label_for(db, t0, t1, source=None):
    """'Music' only if a song was identified on this mic in the span, or the audio truly is music; spoken media
    (TV, YouTube, Cast) is 'TV', with the Cast title when the room TV says what is on. Fred 8:08 AM: "Its reporting
    Music playing but its tv. very different." (the old label read every mic's windows and let YAMNet's broad music
    class win over a narrated video)."""
    song = db.execute("""SELECT title, artist FROM media_tracks WHERE status='found' AND source IS ? AND ts0<=? AND ts1>=?
                         ORDER BY score DESC LIMIT 1""", (source, t1, t0)).fetchone()
    if song:
        return "Music", f"{song['title']}" + (f" by {song['artist']}" if song["artist"] else "")
    title = None
    if source is None:
        p = db.execute("""SELECT title, player FROM media_plays WHERE source='cast' AND started<=? AND COALESCE(ended, last_seen)>=?
                          ORDER BY last_seen DESC LIMIT 1""", (t1 + 30, t0 - 30)).fetchone()
        if p:
            title = p["title"] if not p["title"].startswith("(") else p["player"].split(" · ")[-1]
    r = db.execute("SELECT AVG(music) mu, AVG(speech) sp FROM media_windows WHERE ts1>=? AND ts0<=? AND source IS ?", (t0, t1, source)).fetchone()
    if not title and r and r["mu"] is not None and r["mu"] >= MUSIC_MIN and (r["sp"] or 0) < MUSIC_MAX_SPEECH:
        return "Music", None
    if title or (r and r["mu"] is not None) or db.execute("SELECT 1 FROM segments WHERE source IS ? AND ts<? AND ts+dur>? AND media IN (1,2) LIMIT 1",
                                                          (source, t1, t0)).fetchone():
        return "TV", title          # transcribed speech that is media = spoken media
    return "Media", None
