"""Whisper hallucination guards (E3, Fred 7:57 AM): repetition loops, impossible speech rates, and lone filler words
on long segments. 2026-10-24: line 78 was 1783 chars from 7.7 s ("The big things, the big things, ..."), 97 was
743 chars of "Shh." from 2.8 s, and 10-25 s segments came back as just "the" or "I".

check(text, dur) -> (text, noise, reason). Loops are collapsed to one copy + "…" (the original stays in text_orig);
a line that is still impossible after that, or a lone filler word on a long segment, is marked noise (hidden, never
deleted)."""
import re
import zlib

COMPRESSION_MAX = 2.4      # Whisper's own hallucination threshold (gzip ratio of the text)
MAX_CPS = 35.0             # chars/s of audio; speech is ~15, fast speech on a tight VAD cut reaches ~29 (real lines seen)
MIN_REPEATS = 3            # a phrase said this many times in a row is a loop
FILLER = {"the", "i", "you", "a", "an", "and", "so", "uh", "um", "oh", "it", "to", "in", "of", "yeah"}
FILLER_MIN_SECONDS = 10.0


def compression_ratio(text):
    b = text.encode()
    return len(b) / max(1, len(zlib.compress(b)))


def _norm(w):
    return re.sub(r"[^\w']", "", w.lower())


def collapse(text, max_n=12):
    """Collapse any run of the same 1..max_n word phrase repeated >= MIN_REPEATS times to one copy + '…'."""
    words = text.split()
    out, i, changed = [], 0, False
    while i < len(words):
        best = None
        for n in range(1, min(max_n, (len(words) - i) // MIN_REPEATS) + 1):
            unit = [_norm(w) for w in words[i:i + n]]
            if not any(unit):
                continue
            k = 1
            while words[i + k * n:i + (k + 1) * n] and [_norm(w) for w in words[i + k * n:i + (k + 1) * n]] == unit:
                k += 1
            if k >= MIN_REPEATS and (best is None or k * n > best[0] * best[1]):
                best = (k, n)
        if best:
            k, n = best
            out.extend(words[i:i + n])
            out[-1] = out[-1].rstrip(",;") + " …"
            i += k * n
            changed = True
        else:
            out.append(words[i])
            i += 1
    return " ".join(out), changed


def check(text, dur):
    t = " ".join((text or "").split())
    reasons = []
    if t and (compression_ratio(t) > COMPRESSION_MAX or len(t) > 40):
        c, changed = collapse(t)
        if changed:
            t = c
            reasons.append("loop collapsed")
    noise = False
    if len(t) > 40 and len(t) / max(dur, 0.1) > MAX_CPS:
        noise = True
        reasons.append(f"{len(t) / max(dur, 0.1):.0f} chars/s")
    elif t and compression_ratio(t) > COMPRESSION_MAX and len(t) > 60:
        noise = True
        reasons.append("still repetitive")
    words = [_norm(w) for w in t.split() if _norm(w)]
    if dur >= FILLER_MIN_SECONDS and len(words) <= 1 and (not words or words[0] in FILLER):
        noise = True
        reasons.append(f"lone '{t}' on {dur:.0f} s")
    return t, noise, ", ".join(reasons)
