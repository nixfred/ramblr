"""One way to ask a model, shared by the nightly pass and ask-your-transcript.

auto  = local Ollama only if the GPU is idle and the model is already resident (or VRAM is clearly free); the
        This Feels Like Cheating course bot always has priority. Any local failure or timeout falls through to
        the logged-in Claude Code CLI, run as Ramblr's own niced job (see storyboard.gen_claude).
num_ctx is never sent: a different value would force a reload of a shared model."""
import json
import subprocess
import time
import urllib.request

from . import config, storyboard


class Unavailable(Exception):
    pass


def ollama_ok():
    """(ok, why). nvidia-smi load first, then `ollama ps` for what is resident."""
    util = storyboard.gpu_busy()
    if util is None:
        return False, "nvidia-smi unavailable"
    if util > config.SB_OLLAMA_MAX_GPU:
        return False, f"GPU busy ({util:.0f}%), course bot has priority"
    try:
        with urllib.request.urlopen(config.SB_OLLAMA_URL + "/api/ps", timeout=5) as r:
            loaded = [m["name"] for m in json.loads(r.read()).get("models", [])]
    except Exception as e:
        return False, f"ollama ps failed: {e}"
    if config.SB_OLLAMA_MODEL in loaded:
        return True, f"{config.SB_OLLAMA_MODEL} resident, GPU {util:.0f}%"
    try:
        free = float(subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                                    capture_output=True, text=True, timeout=5).stdout.split()[0])
    except Exception:
        return False, "VRAM unknown"
    if free < 9000:
        return False, f"{config.SB_OLLAMA_MODEL} not resident and only {free:.0f} MiB VRAM free"
    return True, f"VRAM free {free:.0f} MiB, GPU {util:.0f}%"


def generate(prompt, mode="auto", claude_model=None, timeout=120, want_json=True):
    """Returns (text, generator, model, seconds, note)."""
    notes = []
    t = time.monotonic()
    if mode in ("auto", "ollama"):
        ok, why = ollama_ok()
        notes.append(f"ollama: {why}")
        if ok:
            try:
                # short leash: a busy Ollama queue (the course bot) means a timeout, so fail over fast
                text, model = storyboard.gen_ollama(prompt, timeout=min(timeout, config.OLLAMA_LEASH), fmt="json" if want_json else None,
                                                    num_predict=3000)
                return text, "ollama", model, round(time.monotonic() - t, 2), "; ".join(notes)
            except Exception as e:
                notes.append(f"ollama failed: {str(e)[:120]}")
        if mode == "ollama":
            raise Unavailable("; ".join(notes))
    t = time.monotonic()
    try:
        text, model = storyboard.gen_claude(prompt, model=claude_model, timeout=timeout)
        return text, "claude", model, round(time.monotonic() - t, 2), "; ".join(notes)
    except Exception as e:
        # B4-15 offline fallback: Claude unreachable (network, quota, CLI missing) -> the local model, but only under
        # the same GPU rule (nvidia-smi + ollama ps; the course bot first; num_ctx never sent), with a longer leash.
        notes.append(f"claude failed: {str(e)[:120]}")
        ok, why = ollama_ok()
        notes.append(f"offline fallback: {why}")
        if not ok:
            raise Unavailable("; ".join(notes))
        t = time.monotonic()
        text, model = storyboard.gen_ollama(prompt, timeout=config.OFFLINE_LEASH, fmt="json" if want_json else None, num_predict=3000)
        return text, "ollama-offline", model, round(time.monotonic() - t, 2), "; ".join(notes)
