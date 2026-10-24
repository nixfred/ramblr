"""Custom vocabulary learned from corrections (B3-1).

Fred fixes a line's text on the site (the original Whisper text is kept forever in segments.text_orig and every
edit in text_edits). Each fix is diffed word by word; a replaced run of words becomes a learned pair
("rambler" -> "Ramblr"). Pairs plus the words Fred adds by hand form the vocabulary, and every new line is passed
through it right after transcription (whole words, case-insensitive; the raw text is kept in text_orig).

Whisper's initial_prompt would be the natural place for the vocabulary, but the NPU's static Whisper decoder in
OpenVINO GenAI 2026.3.1 rejects initial_prompt and hotwords (shape check), so the prompt is only tried while it
works (RAMBLR_VOCAB_PROMPT=1) and the post-ASR pass always runs."""
import difflib
import json
import re
import time

from . import db as dbm

MAX_PAIR_WORDS = 4


def _bump(db):
    """Every vocabulary change invalidates the cached rules in every process (count/max id miss edits)."""
    dbm.set_meta(db, "vocab_rev", str(int(dbm.get_meta(db, "vocab_rev", "0") or 0) + 1))


def pairs_from_edit(old, new):
    """[(wrong, right)] word runs that changed between Whisper's text and Fred's fix (short runs only)."""
    a, b = old.split(), new.split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=[w.lower().strip(".,!?") for w in a],
                                                      b=[w.lower().strip(".,!?") for w in b]).get_opcodes():
        if op == "replace" and 0 < i2 - i1 <= MAX_PAIR_WORDS and 0 < j2 - j1 <= MAX_PAIR_WORDS:
            wrong = " ".join(w.strip(".,!?") for w in a[i1:i2])
            right = " ".join(w.strip(".,!?") for w in b[j1:j2])
            if wrong.lower() != right.lower() and len(wrong) > 1:
                out.append((wrong, right))
    return out


def correct_line(db, seg_id, new_text, who="site"):
    """Fred's text fix for one line. Never loses the original; learns pairs; returns what it learned."""
    r = db.execute("SELECT text, text_orig FROM segments WHERE id=?", (seg_id,)).fetchone()
    if not r:
        raise LookupError("no such line")
    new_text = " ".join(new_text.split())[:2000]
    if not new_text:
        raise ValueError("text required")
    if new_text == r["text"]:
        return {"changed": False, "learned": []}
    db.execute("INSERT INTO text_edits(segment_id, old, new, who, created) VALUES(?,?,?,?,?)", (seg_id, r["text"], new_text, who, time.time()))
    db.execute("UPDATE segments SET text_orig=COALESCE(text_orig, text), text=?, noise=? WHERE id=?",
               (new_text, 1 if dbm.is_noise(new_text) else 0, seg_id))
    learned = []
    for wrong, right in pairs_from_edit(r["text"], new_text):
        db.execute("""INSERT INTO vocab(wrong, right, source, hits, active, created) VALUES(?,?,'learned',1,1,?)
                      ON CONFLICT(wrong) DO UPDATE SET right=excluded.right, hits=hits+1, active=1""", (wrong.lower(), right, time.time()))
        learned.append([wrong, right])
    if learned:
        _bump(db)
    return {"changed": True, "learned": learned}


def add_word(db, word, wrong=None):
    """A word Fred wants spelled his way (wrong=None: prompt/prefer only), or an explicit wrong -> right pair."""
    word = " ".join(word.split())[:80]
    if not word:
        raise ValueError("word required")
    key = (wrong or word).lower()
    db.execute("""INSERT INTO vocab(wrong, right, source, hits, active, created) VALUES(?,?,'manual',0,1,?)
                  ON CONFLICT(wrong) DO UPDATE SET right=excluded.right, active=1""", (key, word, time.time()))
    _bump(db)


def set_active(db, vid, active):
    if db.execute("UPDATE vocab SET active=? WHERE id=?", (1 if active else 0, vid)).rowcount == 0:
        raise LookupError("no such entry")
    _bump(db)


_cache = {"rev": None, "rules": []}


def rules(db):
    rev = dbm.get_meta(db, "vocab_rev", "0") + "|" + str(db.execute("SELECT COUNT(*), COALESCE(MAX(id),0), SUM(active) FROM vocab").fetchone()[:])
    if rev != _cache["rev"]:
        rs = []
        for r in db.execute("SELECT wrong, right FROM vocab WHERE active=1 AND wrong != lower(right) ORDER BY length(wrong) DESC"):
            rs.append((re.compile(r"(?<![\w'])" + re.escape(r["wrong"]) + r"(?![\w'])", re.I), r["right"]))
        _cache.update(rev=rev, rules=rs)
    return _cache["rules"]


def apply(db, text):
    """New line text through the vocabulary. Returns (text, changed?)."""
    out = text
    for rx, right in rules(db):
        out = rx.sub(right, out)
    return out, out != text


def prompt_words(db, limit=40):
    """Spellings for Whisper's initial prompt (named people + vocabulary), used only where the device allows it."""
    words = [r[0] for r in db.execute("SELECT right FROM vocab WHERE active=1 ORDER BY hits DESC, id DESC LIMIT ?", (limit,))]
    words += [r[0] for r in db.execute("SELECT name FROM speakers WHERE named=1 AND merged_into IS NULL")]
    seen, out = set(), []
    for w in words:
        if w.lower() not in seen:
            seen.add(w.lower()); out.append(w)
    return out[:limit]
