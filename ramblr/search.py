"""Ask your transcript: semantic search plus Q&A with citations.

Embeddings: nomic-embed-text-v1.5 (the official ONNX already on gus for Larry's recall) on the Intel NPU through
OpenVINO, static shapes per length bucket, compiled blobs cached. Consecutive lines are embedded together as a
chunk (a lone 'yeah' carries no meaning). Only settled lines are indexed, so chunks never change afterwards."""
import json
import logging
import re
import threading
import time
from datetime import datetime

import numpy as np

from . import config, db as dbm, llm, storyboard

log = logging.getLogger("ramblr.search")
BUCKETS = (128, 256, 512)
MODEL_NAME = "nomic-embed-text-v1.5"


class Embedder:
    def __init__(self):
        import openvino as ov
        from tokenizers import Tokenizer
        t = time.monotonic()
        config.EMBED_CACHE.mkdir(parents=True, exist_ok=True)
        core = ov.Core()
        core.set_property({"CACHE_DIR": str(config.EMBED_CACHE)})
        self.tok = Tokenizer.from_file(str(config.EMBED_MODEL_DIR / "tokenizer.json"))
        self.tok.enable_truncation(max_length=BUCKETS[-1])
        self.compiled = {}
        for n in BUCKETS:
            m = core.read_model(str(config.EMBED_MODEL_DIR / "onnx" / "model.onnx"))
            m.reshape({i.get_any_name(): [1, n] for i in m.inputs})
            self.compiled[n] = core.compile_model(m, config.EMBED_DEVICE)
        self.lock = threading.Lock()
        self.device = config.EMBED_DEVICE
        log.warning("embedder %s ready on %s in %.1fs", MODEL_NAME, self.device, time.monotonic() - t)

    def embed(self, text):
        ids = self.tok.encode(text).ids
        n = next(b for b in BUCKETS if len(ids) <= b)
        pad = n - len(ids)
        input_ids = np.array([ids + [0] * pad], dtype=np.int64)
        mask = np.array([[1] * len(ids) + [0] * pad], dtype=np.int64)
        model = self.compiled[n]
        feed = {}
        for i in model.inputs:
            name = i.get_any_name()
            feed[name] = input_ids if "input_ids" in name else mask if "attention_mask" in name else np.zeros((1, n), dtype=np.int64)
        with self.lock:
            hidden = model(feed)[model.outputs[0]]
        w = mask[..., None].astype(np.float32)
        v = ((hidden * w).sum(1) / w.sum(1))[0]          # mean pooling over real tokens
        return (v / (np.linalg.norm(v) or 1)).astype(np.float32)


_emb = {"obj": None, "used": 0.0}
_emb_lock = threading.Lock()


def embedder():
    with _emb_lock:
        if _emb["obj"] is None:
            _emb["obj"] = Embedder()
        _emb["used"] = time.monotonic()
        return _emb["obj"]


def unload_if_idle():
    """The web process calls this periodically: the model (~550 MB) is freed after EMBED_IDLE_UNLOAD seconds idle."""
    with _emb_lock:
        if _emb["obj"] is not None and time.monotonic() - _emb["used"] > config.EMBED_IDLE_UNLOAD:
            _emb["obj"] = None
            return True
    return False


def index_pending(db, limit=20000):
    """Embed settled lines that are not indexed yet. Returns counts."""
    hw = int(dbm.get_meta(db, "index_seg_id", "0"))
    rows = db.execute("SELECT id, ts, dur, day, text, media FROM segments WHERE id>? AND ts<? ORDER BY id LIMIT ?",
                      (hw, time.time() - config.CHUNK_SETTLE, limit)).fetchall()
    if not rows:
        return {"chunks": 0, "lines": 0}
    groups, cur = [], []
    for r in rows:
        if r["text"] and not dbm.is_noise(r["text"]) and r["media"] not in (1, 2):   # TV/music lines are not indexed
            if cur and (r["day"] != cur[-1]["day"] or r["ts"] - (cur[-1]["ts"] + cur[-1]["dur"]) > config.CHUNK_GAP
                        or sum(len(x["text"]) for x in cur) + len(r["text"]) > config.CHUNK_CHARS):
                groups.append(cur)
                cur = []
            cur.append(r)
    if cur:
        groups.append(cur)
    e = embedder() if groups else None
    t = time.monotonic()
    for g in groups:
        v = e.embed("search_document: " + " ".join(x["text"] for x in g))
        db.execute("""INSERT INTO chunks(first_seg,last_seg,day,ts0,ts1,seg_ids,vec,model) VALUES(?,?,?,?,?,?,?,?)
                      ON CONFLICT(first_seg) DO UPDATE SET last_seg=excluded.last_seg, seg_ids=excluded.seg_ids,
                      vec=excluded.vec, ts1=excluded.ts1""",
                   (g[0]["id"], g[-1]["id"], g[0]["day"], g[0]["ts"], g[-1]["ts"] + g[-1]["dur"],
                    json.dumps([x["id"] for x in g]), dbm.vec_to_blob(v), MODEL_NAME))
    dbm.set_meta(db, "index_seg_id", rows[-1]["id"])
    _matrix["n"] = -1
    return {"chunks": len(groups), "lines": len(rows), "seconds": round(time.monotonic() - t, 2),
            "device": e.device if e else None}


_matrix = {"n": -1, "ids": None, "m": None}


def matrix(db):
    n = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if n != _matrix["n"]:
        rs = db.execute("SELECT id, vec FROM chunks ORDER BY id").fetchall()
        _matrix.update(n=n, ids=np.array([r["id"] for r in rs], dtype=np.int64),
                       m=np.stack([dbm.blob_to_vec(r["vec"]) for r in rs]) if rs else np.zeros((0, 768), np.float32))
    return _matrix["ids"], _matrix["m"]


def semantic(db, query, k=12, start=None, end=None, speaker_id=None):
    """Top chunks for a question: [{chunk_id, score, seg_ids, day, ts0}] after optional filters."""
    ids, m = matrix(db)
    if not len(ids):
        return []
    q = embedder().embed("search_query: " + query)
    scores = m @ q
    out = []
    for i in np.argsort(-scores):
        c = db.execute("SELECT id, seg_ids, day, ts0, ts1 FROM chunks WHERE id=?", (int(ids[i]),)).fetchone()
        if (start and c["ts1"] < start) or (end and c["ts0"] > end):
            continue
        seg_ids = json.loads(c["seg_ids"])
        if speaker_id:
            ph = ",".join("?" * len(seg_ids))
            if not db.execute(f"SELECT 1 FROM segments WHERE id IN ({ph}) AND speaker_id=? LIMIT 1", seg_ids + [speaker_id]).fetchone():
                continue
        out.append({"chunk_id": c["id"], "score": round(float(scores[i]), 4), "seg_ids": seg_ids, "day": c["day"], "ts0": c["ts0"]})
        if len(out) >= k:
            break
    return out


ASK = """You answer Fred's question using ONLY the transcript excerpts below (lines from his room, with ids).
Be direct and short (a few sentences or a tight list). After each claim cite the line ids in square brackets like [#123].
If the excerpts do not contain the answer, say so plainly; do not guess.
Today is {today}.

QUESTION: {q}

EXCERPTS (#id [date time] speaker: text):
{ctx}
"""


def ask(db, question, k=10):
    t = time.monotonic()
    try:
        index_pending(db)
    except Exception:
        log.exception("index before ask failed; answering from the existing index")
    seg_ids, h = hybrid(db, question, k=max(k, 16))
    seg_ids = sorted(set(seg_ids))
    if not seg_ids:
        return {"answer": "Nothing in the transcript matches that yet.", "citations": [], "model": None}
    ph = ",".join("?" * len(seg_ids))
    rows = db.execute(f"""SELECT s.id, s.ts, s.day, s.text, COALESCE(sp.name,'Unknown') AS speaker FROM segments s
                          LEFT JOIN speakers sp ON sp.id=s.speaker_id WHERE s.id IN ({ph}) AND COALESCE(s.media,0) NOT IN (1,2)
                          ORDER BY s.ts""", seg_ids).fetchall()
    ctx = "\n".join(f"#{r['id']} [{datetime.fromtimestamp(r['ts']).strftime('%Y-%m-%d %H:%M')}] {r['speaker']}: {r['text']}" for r in rows)
    scope = ""
    if h["start"]:
        scope = f"The question is about {datetime.fromtimestamp(h['start']):%Y-%m-%d %H:%M} to {datetime.fromtimestamp(h['end']):%Y-%m-%d %H:%M}.\n"
    prompt = ASK.replace("{today}", datetime.now().strftime("%Y-%m-%d %A") + "\n" + scope).replace("{q}", question).replace("{ctx}", ctx[:60000])
    text, gen, model, secs, note = llm.generate(prompt, mode=config.ASK_GEN, claude_model=config.ASK_CLAUDE_MODEL,
                                                timeout=config.ASK_TIMEOUT, want_json=False)
    cited = sorted({int(x) for x in re.findall(r"#(\d+)", text)} & set(seg_ids))
    by_id = {r["id"]: r for r in rows}
    return {"answer": text.strip(), "model": model, "generator": gen, "seconds": round(time.monotonic() - t, 2),
            "citations": [{"id": i, "day": by_id[i]["day"], "t": datetime.fromtimestamp(by_id[i]["ts"]).strftime("%H:%M:%S"),
                           "speaker": by_id[i]["speaker"], "text": by_id[i]["text"],
                           "playable": config.AUDIO24_ENABLED and time.time() - by_id[i]["ts"] < 86400} for i in cited],
            "scope": {"start": h["start"], "end": h["end"], "speaker_ids": h["speaker_ids"]},
            "retrieved": len(rows)}


# ---- B3-3 smart search: query hints + hybrid (meaning + words) retrieval with reciprocal-rank fusion ----
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def hints(db, q, now=None):
    """Time range and people a question names: ('yesterday', 'last week', 'on Tuesday', 'this morning', names)."""
    from datetime import datetime as dt, timedelta
    now = now or time.time()
    t = dt.fromtimestamp(now)
    day0 = t.replace(hour=0, minute=0, second=0, microsecond=0)
    s = " " + q.lower() + " "
    rng = None
    if " yesterday" in s:
        rng = (day0 - timedelta(days=1), day0)
    elif " today" in s or " this morning" in s or " tonight" in s or " this afternoon" in s or " this evening" in s:
        rng = (day0, day0 + timedelta(days=1))
    elif " last week" in s:
        start = day0 - timedelta(days=day0.weekday() + 7)
        rng = (start, start + timedelta(days=7))
    elif " this week" in s:
        start = day0 - timedelta(days=day0.weekday())
        rng = (start, day0 + timedelta(days=1))
    elif " last month" in s:
        first = day0.replace(day=1)
        prev = (first - timedelta(days=1)).replace(day=1)
        rng = (prev, first)
    else:
        m = re.search(r"\b(\d+|two|three|four|five|six|seven)\s+days?\s+ago\b", s)
        if m:
            n = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}.get(m.group(1)) or int(m.group(1))
            rng = (day0 - timedelta(days=n), day0 - timedelta(days=n - 1))
        else:
            for i, d in enumerate(WEEKDAYS):
                if re.search(rf"\b(on |last )?{d}\b", s):
                    back = (day0.weekday() - i) % 7 or 7
                    rng = (day0 - timedelta(days=back), day0 - timedelta(days=back - 1))
                    break
    part = None
    for word, (h0, h1) in (("morning", (5, 12)), ("afternoon", (12, 17)), ("evening", (17, 23)), ("tonight", (17, 24))):
        if word in s and rng:
            part = (rng[0].replace(hour=h0), rng[0].replace(hour=h0) + timedelta(hours=h1 - h0))
    if part:
        rng = part
    people = []
    for r in db.execute("SELECT id, name FROM speakers WHERE named=1 AND merged_into IS NULL"):
        first = r["name"].split()[0].lower()
        if re.search(rf"\b{re.escape(first)}\b", s) or re.search(rf"\b{re.escape(r['name'].lower())}\b", s):
            people.append(r["id"])
    return {"start": rng[0].timestamp() if rng else None, "end": rng[1].timestamp() if rng else None, "speaker_ids": people}


STOP = set("a an the and or but of to in on at for with what who when where why how did do does was were is are i me my we you "
           "about that this it he she they them his her their there said say tell any anything last week yesterday today".split())


def fts_query(q):
    words = [w for w in re.findall(r"[a-z0-9']+", q.lower()) if w not in STOP and len(w) > 1]
    return " OR ".join('"' + w.replace('"', "") + '"' for w in words[:12])


def hybrid(db, q, k=24):
    """Ranked transcript line ids for a question: semantic chunks and FTS5 word hits fused by reciprocal rank,
    restricted by the question's time range and people, media lines excluded, with one line of context each side."""
    h = hints(db, q)
    rank = {}

    def add(ids, weight=1.0):
        for i, sid in enumerate(ids):
            rank[sid] = rank.get(sid, 0.0) + weight / (60 + i)

    try:
        sem = semantic(db, q, k=k, start=h["start"], end=h["end"])
        add([sid for c in sem for sid in c["seg_ids"]])
    except Exception:
        log.exception("semantic search failed; words only")
    fq = fts_query(q)
    if fq:
        where, args = ["segments_fts MATCH ?"], [fq]
        if h["start"]:
            where.append("s.ts>=? AND s.ts<?"); args += [h["start"], h["end"]]
        rows = db.execute(f"""SELECT s.id FROM segments_fts f JOIN segments s ON s.id=f.rowid WHERE {' AND '.join(where)}
                              AND s.noise=0 AND COALESCE(s.media,0) NOT IN (1,2) ORDER BY bm25(segments_fts) LIMIT 40""", args).fetchall()
        add([r[0] for r in rows], 1.2)            # exact names and terms count a little more
    if h["speaker_ids"]:
        ph = ",".join("?" * len(h["speaker_ids"]))
        mine = {r[0] for r in db.execute(f"SELECT id FROM segments WHERE speaker_id IN ({ph})", h["speaker_ids"])}
        for sid in rank:
            if sid in mine:
                rank[sid] *= 1.5                  # the person the question names
    top = [sid for sid, _ in sorted(rank.items(), key=lambda x: -x[1])[:k]]
    ctx = set(top)
    for sid in top:                               # a line either side, so answers see the exchange
        for r in db.execute("""SELECT id FROM segments WHERE noise=0 AND COALESCE(media,0) NOT IN (1,2) AND ts BETWEEN
                               (SELECT ts-120 FROM segments WHERE id=?) AND (SELECT ts+120 FROM segments WHERE id=?)
                               ORDER BY ABS(ts-(SELECT ts FROM segments WHERE id=?)) LIMIT 3""", (sid, sid, sid)):
            ctx.add(r[0])
    return sorted(ctx), h
