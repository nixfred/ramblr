"""Whisper on the Intel NPU through openvino_genai (same call pattern as the intel-npu speech server).

Outage 2026-10-24 09:04-11:36: distil-large-v3 rejected the vocabulary initial_prompt (the NPU decoder is static) and
the failed call left the pipeline unusable: every later generate() raised "roi_end <= max_dim". So: no prompts on
the NPU at all, a failed call rebuilds the pipeline and retries once, and if the rebuilt one fails too the service
falls back to whisper-base.en once, alerts Fred and leaves a marker so a restart does not flip back and forth."""
import json
import logging
import os
import time

import numpy as np

from . import config

log = logging.getLogger("ramblr.asr")


def fallback_marker():
    """{"model", "reason", "at"} if a model was given up on (cleared by `python -m ramblr.watchdog clear`)."""
    try:
        return json.loads(config.ASR_FALLBACK_FILE.read_text())
    except (OSError, ValueError):
        return None


def mark_fallback(model, reason):
    config.ASR_FALLBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.ASR_FALLBACK_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"model": model, "reason": reason[:300], "at": time.time()}))
    os.replace(tmp, config.ASR_FALLBACK_FILE)


class NpuWhisper:
    def __init__(self, model_dir=None, device=None, fallback=True):
        self.model_dir = model_dir or config.WHISPER_DIR
        self.device = device or config.WHISPER_DEVICE
        self.fallback_dir = config.WHISPER_FALLBACK if fallback and config.WHISPER_FALLBACK != self.model_dir else None
        self.configured = self.model_dir.name
        self.errors = 0              # failed generate() calls since start (published as meta asr_errors)
        self.fell_back = None        # reason, once this process gave up on the configured model
        m = fallback_marker() if self.fallback_dir else None
        if m and m.get("model") == self.configured:
            log.error("Whisper %s was given up on (%s); running %s. Clear with: python -m ramblr.watchdog clear",
                      self.configured, m.get("reason", "?"), self.fallback_dir.name)
            self.fell_back = m.get("reason", "marker")
            self.model_dir, self.fallback_dir = self.fallback_dir, None
        config.WHISPER_CACHE.mkdir(parents=True, exist_ok=True)
        self._load()

    def _make_pipe(self, model_dir):
        import openvino_genai as genai
        return genai.WhisperPipeline(str(model_dir), self.device, CACHE_DIR=str(config.WHISPER_CACHE))

    def _load(self):
        t = time.monotonic()
        self.pipe = self._make_pipe(self.model_dir)
        self.name = self.model_dir.name
        log.warning("Whisper %s ready on %s in %.1fs", self.name, self.device, time.monotonic() - t)

    prompt = ""                  # B3-1: vocabulary prompt, only while the device accepts it
    # the NPU's static decoder never accepts one, and a rejected prompt poisons distil's pipeline: off on the NPU
    prompt_ok = None

    def _prompt_on(self):
        if self.prompt_ok is None:
            env = os.environ.get("RAMBLR_VOCAB_PROMPT", "auto")
            self.prompt_ok = env == "1" or (env == "auto" and self.device != "NPU")
        return self.prompt_ok

    def _generate(self, a):
        if self.prompt and self._prompt_on():
            try:
                cfg = self.pipe.get_generation_config()
                cfg.max_new_tokens = config.ASR_MAX_TOKENS
                cfg.initial_prompt = self.prompt
                return self.pipe.generate(a, cfg)
            except Exception as e:
                self.prompt_ok = False      # this device rejects prompts: stop trying, vocab runs after ASR
                log.warning("Whisper on %s does not take an initial prompt (%s); vocabulary is applied after transcription",
                            self.device, str(e).splitlines()[0][:120])
                self._load()                # a rejected call can leave the pipeline unusable: start clean
        return self.pipe.generate(a, max_new_tokens=config.ASR_MAX_TOKENS)

    def transcribe(self, audio):
        """audio: float32 mono 16 kHz. Returns (text, elapsed_ms)."""
        if len(audio) == 0 or float(np.max(np.abs(audio))) < 1e-4:
            return "", 0
        t = time.monotonic()
        a = np.ascontiguousarray(audio, dtype=np.float32)
        try:
            res = self._generate(a)
        except Exception as e:
            self.errors += 1
            why = str(e).strip().splitlines()[-1][:200] if str(e).strip() else type(e).__name__
            log.error("Whisper %s failed on a %.1fs segment: %s; rebuilding the pipeline", self.name, len(a) / config.RATE, why)
            try:
                self._load()
                res = self._generate(a)
            except Exception as e2:
                self.errors += 1
                if not self.fallback_dir:
                    raise
                why2 = str(e2).strip().splitlines()[-1][:200] if str(e2).strip() else type(e2).__name__
                reason = f"{self.name} failed twice, also after a rebuild: {why2 or why}"
                log.error("FALLING BACK to %s: %s", self.fallback_dir.name, reason)
                mark_fallback(self.configured, reason)
                self.model_dir, self.fallback_dir, self.fell_back = self.fallback_dir, None, reason
                self._load()
                from . import watchdog
                watchdog.alert(f"Ramblr fell back to {self.name}", reason)
                res = self._generate(a)
        return "".join(res.texts).strip(), int((time.monotonic() - t) * 1000)
