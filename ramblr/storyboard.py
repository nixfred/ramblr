"""Idea storyboards: a real product storyboard for an idea, not a summary of what was said.
Runtime generator is a config switch: local Ollama (only when the GPU is idle, the course bot has priority)
or the already-logged-in Claude Code CLI as Ramblr's own background job. No API key is involved.
Every build adds a storyboard VERSION; nothing is overwritten or deleted."""
import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime

from . import config, db as dbm, mockups, notify

SCHEMA_HINT = """{
 "title": "punchy title",
 "pitch": "one line",
 "problem": {"text": "...", "for_whom": "..."},
 "vision": "what it looks like when it works",
 "panels": [{"caption": "short caption", "scene": "what the screen/user is doing in this step"}],
 "features": {"mvp": ["..."], "later": ["..."]},
 "build": {"machine": "gus|vic|fnix", "who": "which bot or Larry", "effort": "rough effort", "stack": "how on Fred's stack"},
 "risks": ["..."],
 "open_questions": ["..."],
 "quotes": [{"ts": "HH:MM:SS", "text": "verbatim from the transcript"}],
 "first_steps": ["step 1", "step 2", "step 3"],
 "score": {"value": 1-10, "verdict": "worth doing | maybe | skip", "why": "..."}
}"""

PROMPT = """You turn a spoken idea into an IDEA STORYBOARD for Fred (AI infrastructure architect; machines: gus = RTX 5070 Arch desktop-laptop, vic = RTX 4050 Arch laptop, fnix = MacBook Pro M4 Max; he works with Larry, a Claude-based engineering collaborator, plus other bots; stack: TypeScript on bun, Python via uv, Markdown over HTML).

This is NOT a summary of what was said. Design the idea: infer the product from the rough, rambling speech and make it concrete.
Return ONLY one JSON object, no prose, no code fence, exactly this shape:
{schema}

Rules:
- "panels": 4 to 8 storyboard panels walking the UX step by step; each caption short (under 12 words); "scene" one or two sentences.
- "quotes": 1 to 4 of the best lines Fred actually said, copied VERBATIM from the transcript below (same words), with their ts.
- "build.machine": pick gus, vic or fnix and say why inside "stack"; "build.effort" is rough (hours/days), honest.
- "score.value" is a gut check 1-10; "verdict" is one of: worth doing, maybe, skip; "why" is blunt and specific.
- "first_steps": exactly 3 concrete steps.
- If the transcript is thin, still design the most plausible version and list what is unclear under "open_questions".

TRANSCRIPT (speaker, time, text):
{lines}
"""


def gather(db, idea_id):
    rows = db.execute("""SELECT s.id, s.ts, s.text, COALESCE(sp.name,'Unknown') AS speaker FROM idea_lines il
                         JOIN segments s ON s.id=il.segment_id LEFT JOIN speakers sp ON sp.id=s.speaker_id
                         WHERE il.idea_id=? ORDER BY s.ts, s.id""", (idea_id,)).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "t": datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S"),
             "speaker": r["speaker"], "text": r["text"]} for r in rows]


def build_prompt(lines):
    txt = "\n".join(f"[{l['t']}] {l['speaker']}: {l['text']}" for l in lines)
    return PROMPT.replace("{schema}", SCHEMA_HINT).replace("{lines}", txt)


def gpu_busy():
    """GPU utilisation sampled over ~3 s (max of 3 samples), None if nvidia-smi is unavailable."""
    vals = []
    try:
        for _ in range(3):
            o = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
            vals.append(float(o.splitlines()[0]))
            time.sleep(1)
    except Exception:
        return None
    return max(vals)


def gen_ollama(prompt, timeout=None, fmt="json", num_predict=2500):
    """Local model. Defers (raises Busy) while the GPU is loaded: the This Feels Like Cheating bot has priority.
    num_ctx is never sent: the loaded runner keeps its own (a different value would force a reload of a shared model)."""
    util = gpu_busy()
    if util is not None and util > config.SB_OLLAMA_MAX_GPU:
        raise Busy(f"GPU busy ({util:.0f}% > {config.SB_OLLAMA_MAX_GPU:.0f}%), course bot has priority")
    req_body = {"model": config.SB_OLLAMA_MODEL, "prompt": prompt, "stream": False,
                "options": {"temperature": 0.7, "num_predict": num_predict}}
    if fmt:
        req_body["format"] = fmt
    body = json.dumps(req_body).encode()
    req = urllib.request.Request(config.SB_OLLAMA_URL + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout or config.SB_TIMEOUT) as r:
        return json.loads(r.read())["response"], config.SB_OLLAMA_MODEL


def gen_claude(prompt, model=None, timeout=None):
    """Claude Code CLI logged in on this box, run as Ramblr's own niced job from a neutral cwd with only
    project-level settings, so none of the user's hooks, plugins or memory fire for it."""
    config.SB_CLAUDE_CWD.mkdir(parents=True, exist_ok=True)
    model = model or config.SB_CLAUDE_MODEL
    p = subprocess.run(["nice", "-n", "10", "claude", "-p", "--model", model,
                        "--output-format", "json", "--no-session-persistence", "--setting-sources", "project",
                        "--disable-slash-commands", "--strict-mcp-config", "--tools", ""],
                       input=prompt, capture_output=True, text=True, timeout=timeout or config.SB_TIMEOUT, cwd=config.SB_CLAUDE_CWD)
    if p.returncode != 0:
        raise RuntimeError(f"claude -p exit {p.returncode}: {p.stderr.strip()[:300]}")
    out = json.loads(p.stdout)
    res = out if isinstance(out, dict) else next((x for x in out if x.get("type") == "result"), {})
    if res.get("is_error") or "result" not in res:
        raise RuntimeError(f"claude -p error: {str(res.get('result'))[:300]}")
    return res["result"], next(iter(res.get("modelUsage") or {}), model)


class Busy(Exception):
    pass


def parse_json(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b < a:
        raise ValueError("no JSON object in model output")
    return json.loads(t[a:b + 1])


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", str(s).lower())).strip()


def normalize(obj, lines):
    """Coerce to the stored shape; quotes must be verbatim from the raw lines (anything else is dropped and
    replaced by real lines); every panel gets an empty image slot (images are never generated here)."""
    def lst(v):
        return [str(x) for x in v] if isinstance(v, list) else []
    prob = obj.get("problem") if isinstance(obj.get("problem"), dict) else {"text": str(obj.get("problem", "")), "for_whom": ""}
    feats = obj.get("features") if isinstance(obj.get("features"), dict) else {}
    build = obj.get("build") if isinstance(obj.get("build"), dict) else {}
    score = obj.get("score") if isinstance(obj.get("score"), dict) else {}
    panels = []
    for p in (obj.get("panels") or [])[:8]:
        if isinstance(p, dict) and (p.get("caption") or p.get("scene")):
            panels.append({"n": len(panels) + 1, "caption": str(p.get("caption", "")), "scene": str(p.get("scene", "")), "image": None})
    raw = {_norm(l["text"]): l for l in lines}
    quotes = []
    for q in obj.get("quotes") or []:
        txt = q.get("text", "") if isinstance(q, dict) else str(q)
        n = _norm(txt)
        if n and any(n in r for r in raw):
            hit = next(l for r, l in raw.items() if n in r)
            quotes.append({"ts": hit["t"], "speaker": hit["speaker"], "text": txt.strip()})
    if not quotes:
        quotes = [{"ts": l["t"], "speaker": l["speaker"], "text": l["text"]} for l in sorted(lines, key=lambda l: -len(l["text"]))[:2]]
    try:
        val = max(1, min(10, int(round(float(score.get("value", 5))))))
    except (TypeError, ValueError):
        val = 5
    out = {"title": str(obj.get("title", "")).strip() or "Untitled idea", "pitch": str(obj.get("pitch", "")),
           "problem": {"text": str(prob.get("text", "")), "for_whom": str(prob.get("for_whom", ""))},
           "vision": str(obj.get("vision", "")), "panels": panels,
           "features": {"mvp": lst(feats.get("mvp")), "later": lst(feats.get("later"))},
           "build": {k: str(build.get(k, "")) for k in ("machine", "who", "effort", "stack")},
           "risks": lst(obj.get("risks")), "open_questions": lst(obj.get("open_questions")), "quotes": quotes,
           "first_steps": lst(obj.get("first_steps"))[:3],
           "score": {"value": val, "verdict": str(score.get("verdict", "maybe")), "why": str(score.get("why", ""))}}
    if len(out["panels"]) < 4:
        raise ValueError(f"only {len(out['panels'])} storyboard panels, need 4 to 8")
    return out


def run_text(prompt, g):
    if g == "ollama":
        return gen_ollama(prompt)
    try:
        return gen_claude(prompt)
    except Exception as e:      # B4-15: offline (or out of quota) -> local model, still only when the GPU is idle
        from . import llm
        ok, why = llm.ollama_ok()
        if not ok:
            raise RuntimeError(f"claude unavailable ({str(e)[:120]}) and no local fallback: {why}")
        return gen_ollama(prompt, timeout=config.OFFLINE_LEASH)


def generate_mockups(sb, g):
    """Second pass with the same generator that made the storyboard. Returns (list, seconds)."""
    brief = {k: sb[k] for k in ("title", "pitch", "problem", "vision", "panels", "features")}
    t = time.monotonic()
    text, _ = run_text(mockups.PROMPT + json.dumps(brief, indent=1), g)
    ms = mockups.parse(text)
    if len(ms) < 2:
        raise ValueError(f"only {len(ms)} usable mockups")
    return ms, time.monotonic() - t


def generate(lines, gen=None):
    """Returns (storyboard dict, generator name, model id, seconds). gen: 'ollama' | 'claude' | 'auto' (default config)."""
    gen = gen or config.SB_GEN
    prompt = build_prompt(lines)
    order = {"ollama": ["ollama"], "claude": ["claude"], "auto": ["ollama", "claude"]}[gen]
    last = None
    for g in order:
        t = time.monotonic()
        try:
            text, model = run_text(prompt, g)
            return normalize(parse_json(text), lines), g, model, time.monotonic() - t
        except Busy as e:
            last = e
        except Exception as e:
            last = e
            if gen != "auto":
                raise
    raise last


def recover_stale(db):
    """A build interrupted by a crash or restart stays 'building' forever and blocks the idea; mark it failed so
    the next request (or the quiet-period auto-run) can build a fresh version. Nothing is deleted."""
    cut = time.time() - (2 * config.SB_TIMEOUT + 120)
    n = db.execute("UPDATE storyboards SET status='error', error='interrupted (worker restarted mid-build)' "
                   "WHERE status='building' AND created < ?", (cut,)).rowcount
    return n


def next_version(db, idea_id):
    return (db.execute("SELECT COALESCE(MAX(version),0) FROM storyboards WHERE idea_id=?", (idea_id,)).fetchone()[0]) + 1


def build(db, idea_id, rebuild=False, gen=None):
    """Idempotent: with a storyboard already present only rebuild=True adds a new version; a build in progress is
    never duplicated. Returns the storyboard row id (or None if skipped)."""
    db.execute("BEGIN IMMEDIATE")
    try:
        cur = db.execute("SELECT status FROM storyboards WHERE idea_id=? ORDER BY version DESC LIMIT 1", (idea_id,)).fetchone()
        if cur and (cur["status"] == "building" or (cur["status"] == "ok" and not rebuild)):
            db.execute("UPDATE ideas SET build_requested=0 WHERE id=?", (idea_id,))
            db.execute("COMMIT")
            return None
        ver = next_version(db, idea_id)
        sid = db.execute("INSERT INTO storyboards(idea_id,version,status,created) VALUES(?,?, 'building', ?)",
                         (idea_id, ver, time.time())).lastrowid
        db.execute("UPDATE ideas SET build_requested=0, updated=? WHERE id=?", (time.time(), idea_id))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    notify.send({"t": "idea", "id": idea_id})
    lines = gather(db, idea_id)
    try:
        sb, g, model, secs = generate(lines, gen)
        try:
            ms, msecs = generate_mockups(sb, g)
            sb["mockups"] = [{"n": m["n"], "title": m["title"], "html": m["html"]} for m in ms]
            sb["mockup_latency_s"] = round(msecs, 2)
            try:
                mockups.render_thumbs(sid, ms)
            except Exception as e:  # mockups still show in iframes; only the phone thumbnails are missing
                sb["thumb_error"] = f"{type(e).__name__}: {e}"[:300]
        except Exception as e:      # the storyboard stands without mockups; the card shows why
            sb["mockups"], sb["mockup_error"] = [], f"{type(e).__name__}: {e}"[:300]
        db.execute("UPDATE storyboards SET status='ok', generator=?, model=?, latency_s=?, body=? WHERE id=?",
                   (g, model, round(secs, 2), json.dumps(sb), sid))
        db.execute("UPDATE ideas SET title=?, updated=? WHERE id=?", (sb["title"], time.time(), idea_id))
        from . import events
        events.emit(db, "idea.storyboard", {"idea_id": idea_id, "version": ver, "title": sb["title"], "pitch": sb.get("pitch", "")})
    except Exception as e:
        db.execute("UPDATE storyboards SET status='error', error=? WHERE id=?", (f"{type(e).__name__}: {e}"[:500], sid))
    notify.send({"t": "idea", "id": idea_id})
    return sid
