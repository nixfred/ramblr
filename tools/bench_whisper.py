"""Read-only Whisper benchmark on the Intel NPU (G, Fred 8:35 AM: "YES" to BLOCKED #10).

  .venv/bin/python tools/bench_whisper.py clips  OUT.npz [--from ID --to ID]   # decode room clips from the 24 h store
  .venv/bin/python tools/bench_whisper.py run    MODEL_DIR CLIPS.npz OUT.json   # one model, in its own process
  .venv/bin/python tools/bench_whisper.py report CLIPS.npz A.json B.json ...    # numbers-only Markdown

Nothing here writes to the live DB, the live model setting or the live compile cache (RAMBLR_WHISPER_CACHE is pointed
at a scratch dir by the caller). Never the GPU. Clips and transcripts stay out of git: the report carries numbers
plus the public JFK clip side by side, because the repo rule is "no transcripts or audio in git"."""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

JFK = Path.home() / ".local/share/ramblr/test/jfk16.wav"
JFK_REF = "and so my fellow americans ask not what your country can do for you ask what you can do for your country"
RATE = 16000


def norm(t):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", " ", (t or "").lower())).strip()


def wer(ref, hyp):
    r, h = norm(ref).split(), norm(hyp).split()
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)] / max(1, len(r))


def rss_mb():
    for line in open("/proc/self/status"):
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


def npu_mem():
    try:
        return int(open("/sys/class/accel/accel0/device/npu_memory_utilization").read().strip())
    except OSError:
        return None


def clips(out, lo, hi):
    """Room lines lo..hi that still have audio in the 24 h store, decoded to float32 16 kHz, plus their labels."""
    import sqlite3
    from ramblr import audio24
    d = sqlite3.connect("file:" + str(Path.home() / ".local/share/ramblr/ears.db") + "?mode=ro", uri=True)
    d.row_factory = sqlite3.Row
    ids, audio, meta, skipped = [], [], [], 0
    for r in d.execute("""SELECT id, ts, dur, media, noise, text FROM segments WHERE source IS NULL AND id BETWEEN ? AND ?
                          ORDER BY id""", (lo, hi)):
        ogg = audio24.clip(r["ts"], r["dur"], pad=0.0)
        if not ogg:
            continue
        p = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "1", "-ar", str(RATE), "pipe:1"],
                           input=ogg, capture_output=True)
        if p.returncode != 0:
            skipped += 1                    # e.g. a line that straddles a chunk boundary: skipped, counted
            continue
        pcm = p.stdout
        a = np.frombuffer(pcm, np.float32)
        if len(a) < RATE // 2:
            continue
        ids.append(r["id"])
        audio.append(a)
        meta.append({"id": r["id"], "dur": round(len(a) / RATE, 2), "tv": r["media"] in (1, 2), "live_text": r["text"]})
    import soundfile as sf
    jfk = sf.read(str(JFK), dtype="float32")[0]
    np.savez(out, jfk=jfk, **{f"c{i}": a for i, a in zip(ids, audio)})
    Path(out).with_suffix(".meta.json").write_text(json.dumps(meta))
    print(json.dumps({"clips": len(ids), "skipped": skipped, "seconds": round(sum(len(a) for a in audio) / RATE, 1), "tv": sum(m["tv"] for m in meta)}))


def run(model_dir, clips_path, out):
    from ramblr import textguard
    from ramblr.asr import NpuWhisper
    z = np.load(clips_path)
    meta = json.loads(Path(clips_path).with_suffix(".meta.json").read_text())
    rss0, npu0 = rss_mb(), npu_mem()
    t = time.monotonic()
    w = NpuWhisper(model_dir=Path(model_dir), device="NPU", fallback=False)
    load_s = time.monotonic() - t
    rss1, npu1 = rss_mb(), npu_mem()
    jfk_txt, jfk_ms = w.transcribe(z["jfk"])
    res = []
    for m in meta:
        a = z[f"c{m['id']}"]
        txt, ms = w.transcribe(a)
        g_txt, g_noise, why = textguard.check(txt, m["dur"])
        res.append({"id": m["id"], "dur": m["dur"], "tv": m["tv"], "ms": ms, "text": txt,
                    "loop": "loop" in why, "noise": g_noise, "cps": round(len(txt) / max(m["dur"], 0.1), 1)})
    Path(out).write_text(json.dumps({"model": Path(model_dir).name, "load_s": round(load_s, 1), "rss_mb": round(rss1 - rss0),
                                     "npu_mem_delta_mb": round((npu1 - npu0) / 2**20) if npu0 is not None and npu1 is not None else None,
                                     "jfk": {"text": jfk_txt, "ms": jfk_ms, "wer": round(wer(JFK_REF, jfk_txt), 3)}, "clips": res}))
    print(f"{Path(model_dir).name}: load {load_s:.1f}s, {len(res)} clips")


def ref(model_dir, clips_path, out, threads=8):
    """Reference transcripts from a much stronger model on the CPU (nice 19, few threads: never the GPU, never the
    NPU the live services use). There is no hand-corrected text yet, so WER is measured against this reference."""
    import openvino_genai as genai
    os.nice(19 - os.nice(0))
    z = np.load(clips_path)
    meta = json.loads(Path(clips_path).with_suffix(".meta.json").read_text())
    t = time.monotonic()
    pipe = genai.WhisperPipeline(str(model_dir), "CPU", INFERENCE_NUM_THREADS=threads)
    cfg = pipe.get_generation_config()
    cfg.language, cfg.task = "<|en|>", "transcribe"
    res = []
    for m in [{"id": "jfk", "dur": 11.0}] + meta:
        a = np.ascontiguousarray(z["jfk"] if m["id"] == "jfk" else z[f"c{m['id']}"], dtype=np.float32)
        s0 = time.monotonic()
        txt = "".join(pipe.generate(a, cfg).texts).strip()
        res.append({"id": m["id"], "text": txt, "ms": int((time.monotonic() - s0) * 1000)})
    Path(out).write_text(json.dumps({"model": Path(model_dir).name, "seconds": round(time.monotonic() - t), "clips": res}))
    print(f"reference {Path(model_dir).name}: {len(res)} clips in {time.monotonic() - t:.0f}s")


def report(clips_path, *results):
    refs = [json.loads(Path(p).read_text()) for p in results if Path(p).name.startswith("ref-")]
    R = [json.loads(Path(p).read_text()) for p in results if not Path(p).name.startswith("ref-")]
    meta = {m["id"]: m for m in json.loads(Path(clips_path).with_suffix(".meta.json").read_text())}
    out = ["| model | load (s) | RSS (MB) | NPU mem (MB) | JFK WER | RTF all | RTF talk | RTF TV | loops talk | loops TV | >35 cps / noise |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in R:
        def rtf(sel):
            c = [x for x in r["clips"] if sel(x)]
            return round(sum(x["ms"] for x in c) / 1000 / max(1e-9, sum(x["dur"] for x in c)), 3)
        talk = [x for x in r["clips"] if not x["tv"]]
        tv = [x for x in r["clips"] if x["tv"]]
        out.append(f"| {r['model']} | {r['load_s']} | {r['rss_mb']} | {r['npu_mem_delta_mb']} | {r['jfk']['wer']:.3f} | {rtf(lambda x: True)} | "
                   f"{rtf(lambda x: not x['tv'])} | {rtf(lambda x: x['tv'])} | {sum(x['loop'] for x in talk)}/{len(talk)} | "
                   f"{sum(x['loop'] for x in tv)}/{len(tv)} | {sum(x['noise'] for x in r['clips'])}/{len(r['clips'])} |")
    if len(R) >= 2:
        a, b = R[0], R[1]
        bm = {x["id"]: x for x in b["clips"]}
        agree = [wer(x["text"], bm[x["id"]]["text"]) for x in a["clips"] if x["id"] in bm and norm(x["text"])]
        out += ["", f"Word disagreement {a['model']} vs {b['model']} (WER of one against the other, median over clips): "
                    f"{np.median(agree):.2f}" if agree else ""]
    if refs:
        from ramblr import textguard
        rf = {x["id"]: x["text"] for x in refs[0]["clips"]}
        out += ["", f"WER against the reference model {refs[0]['model']} (CPU; no hand-corrected text exists yet). Each model's "
                    "text after the live loop guard (what Ramblr would store), then raw:", "",
                "| model | WER all | WER talk | WER TV | raw WER all | clips where it was clearly better (WER 0.2+ lower) |", "|---|---|---|---|---|---|"]
        per = {}
        for r in R:
            per[r["model"]] = {x["id"]: (wer(rf[x["id"]], textguard.check(x["text"], x["dur"])[0]), wer(rf[x["id"]], x["text"]), x["tv"])
                               for x in r["clips"] if x["id"] in rf and norm(rf[x["id"]])}
        names = list(per)
        for n in names:
            v = per[n]
            def med(sel, k=0):
                xs = sorted(w[k] for w in v.values() if sel(w))
                return round(float(np.mean(xs)), 3) if xs else None
            other = names[1 - names.index(n)] if len(names) == 2 else None
            wins = sum(1 for i, w in v.items() if other and w[0] + 0.2 <= per[other][i][0]) if other else ""
            out.append(f"| {n} | {med(lambda w: True)} | {med(lambda w: not w[2])} | {med(lambda w: w[2])} | {med(lambda w: True, 1)} | {wins} |")
    out += ["", "JFK clip (public, exact reference):", ""]
    for r in R:
        out.append(f"- {r['model']}: \"{r['jfk']['text']}\" (WER {r['jfk']['wer']:.3f}, {r['jfk']['ms']} ms)")
    print("\n".join(out))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "clips":
        lo = int(sys.argv[sys.argv.index("--from") + 1]) if "--from" in sys.argv else 0
        hi = int(sys.argv[sys.argv.index("--to") + 1]) if "--to" in sys.argv else 10**9
        clips(sys.argv[2], lo, hi)
    elif cmd == "run":
        run(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "ref":
        ref(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "report":
        report(sys.argv[2], *sys.argv[3:])
