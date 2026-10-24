"""Regression tests for the confirmed findings of the Codex bug hunt (2026-10-24). Temp DBs only."""
import importlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

JFK = Path.home() / ".local/share/ramblr/test/jfk16.wav"


def speech():
    import soundfile as sf
    if not JFK.exists():
        pytest.skip("needs the JFK clip")
    return sf.read(str(JFK), dtype="float32")[0][:16000 * 4]


def ingest(asr, source=None, spool=False):
    from ramblr import config, pipeline
    importlib.reload(pipeline)
    if not config.VAD_MODEL.exists():
        pytest.skip("needs the Silero model")
    return pipeline.Ingest(config.DB_PATH, asr, spool=spool, media=False, source=source)


def feed_all(ing, clip, t0=1792760000.0):
    for i in range(0, len(clip), 3200):
        ing.feed(clip[i:i + 3200], now=t0 + (i + 3200) / 16000)
    ing.flush(now=t0 + len(clip) / 16000)
    ing.wait_idle()


class Flaky:                       # fails the first call, then works
    name, device = "flaky", "CPU"

    def __init__(self, fails=1):
        self.left = fails

    def transcribe(self, s):
        if self.left:
            self.left -= 1
            raise RuntimeError("NPU hiccup")
        return "hello there", 1


def test_1_transient_asr_failure_is_retried_not_dropped():
    ing = ingest(Flaky(1))
    feed_all(ing, speech())
    assert ing.stored >= 1 and ing.failed == 0


def test_1_spool_failure_keeps_the_text(monkeypatch):
    ing = ingest(Flaky(0), spool=True)
    from ramblr import pipeline
    monkeypatch.setattr(pipeline.sf, "write", lambda *a, **k: (_ for _ in ()).throw(OSError("tmpfs full")))
    feed_all(ing, speech())
    rows = ing.db.execute("SELECT text, audio FROM segments").fetchall()
    assert rows and all(r["text"] == "hello there" and r["audio"] is None for r in rows)


def test_1_hard_failure_is_counted():
    ing = ingest(Flaky(99))
    feed_all(ing, speech())
    assert ing.stored == 0 and ing.failed >= 1


def test_2_3_upload_failure_keeps_file_and_retries_then_gives_up(monkeypatch):
    from ramblr import config, db, uploads
    importlib.reload(uploads)
    d = db.connect(config.DB_PATH)
    uploads.INBOX.mkdir(parents=True, exist_ok=True)
    f = uploads.INBOX / "up-1.wav"
    f.write_bytes(b"RIFF" + b"\0" * 2000)
    uid = d.execute("INSERT INTO uploads(source,name,bytes,mime,who,created,file) VALUES('iPhone','x',2004,'audio/wav','t',?,?)",
                    (time.time(), "up-1.wav")).lastrowid
    monkeypatch.setattr(uploads, "probe", lambda p: (4.0, None))
    monkeypatch.setattr(uploads, "decode", lambda p: np.zeros(16000 * 4, np.float32))

    class BrokenTr:
        def get(self, src):
            raise RuntimeError("Whisper failed to load")
    for attempt in (1, 2):
        uploads.process(d, uid, BrokenTr())
        r = d.execute("SELECT status, attempts FROM uploads WHERE id=?", (uid,)).fetchone()
        assert (r["status"], r["attempts"]) == ("queued", attempt) and f.exists()     # kept for the retry
    uploads.process(d, uid, BrokenTr())
    assert d.execute("SELECT status FROM uploads WHERE id=?", (uid,)).fetchone()[0] == "error" and not f.exists()


def test_6_unidentified_voice_never_queues_a_command():
    from ramblr import config, db, commands, me
    for m in (db, me, commands):
        importlib.reload(m)
    d = db.connect(config.DB_PATH)
    fred = db.new_speaker(d, "Fred", 1)
    d.execute("UPDATE speakers SET confirmed=1 WHERE id=?", (fred,))
    t = 1792760000
    sid, _ = db.insert_segment(d, t, 3.0, "Larry, tell Grok Bot to summarize the deck", None, "t", 1)
    d.execute("UPDATE segments SET media=0, diar=0 WHERE id=?", (sid,))
    assert commands.detect(d, now=t + 3 + 40) == []                      # 40 s: diarizer still pending, gate says wait
    assert d.execute("SELECT COUNT(*) FROM command_intents").fetchone()[0] == 0
    d.execute("UPDATE segments SET diar=1, speaker_id=?, sim=0.9 WHERE id=?", (fred, sid))
    assert len(commands.detect(d, now=t + 3 + 45)) == 1                  # same line, decided once the voice is known


def test_29_continuation_ignores_other_sources_and_backdated_lines():
    from ramblr import config, db, commands
    importlib.reload(db); importlib.reload(commands)
    d = db.connect(config.DB_PATH)
    fred = db.new_speaker(d, "Fred", 1)
    t = 1792760000
    a, _ = db.insert_segment(d, t, 2.0, "Larry, tell vic to rebuild", None, "t", 1)
    b, _ = db.insert_segment(d, t - 3600, 2.0, "the quarterly numbers look bad", None, "t", 1, source="Zoom")
    for s in (a, b):
        d.execute("UPDATE segments SET media=0, diar=1, speaker_id=?, sim=0.9 WHERE id=?", (fred, s))
    commands.detect(d, now=t + 100)
    assert d.execute("SELECT task FROM command_intents").fetchone()[0] == "rebuild"


def test_17_other_sources_are_never_room_media():
    from ramblr import config, db, media
    importlib.reload(media)
    d = db.connect(config.DB_PATH)
    t = 1792760000
    d.execute("INSERT INTO media_windows(ts0,ts1,music,tv,crowd,narration,speech,top,rms) VALUES(?,?,0,0.99,0,0.9,0,518,0.1)", (t - 5, t + 10))
    r, _ = db.insert_segment(d, t, 3.0, "and now the weather", None, "t", 1)
    v, _ = db.insert_segment(d, t, 3.0, "good morning", None, "t", 1, source="mic:vic")
    d.execute("UPDATE segments SET media=NULL WHERE id IN (?,?)", (r, v))
    media.tag_pending(d, until=t + 60)                               # the room's pass: room lines only
    media.tag_pending(d, until=t + 60, source="mic:vic")             # vic's own classifier pass: vic's own windows
    got = dict(d.execute("SELECT id, media FROM segments").fetchall())
    assert got[r] == 1 and got[v] == 0                               # the room's TV never leaks onto vic
    up, _ = db.insert_segment(d, t, 3.0, "uploaded", None, "t", 1, source="iPhone")
    d.execute("UPDATE segments SET media=NULL WHERE id=?", (up,))
    media.tag_pending(d, until=t + 60)
    assert d.execute("SELECT media FROM segments WHERE id=?", (up,)).fetchone()[0] == 0   # no classifier: talk


def test_19_broken_ack_releases_the_device():
    from ramblr import remote
    importlib.reload(remote)
    hub = remote.Hub(asr_factory=lambda: NS(name="s", device="CPU", transcribe=lambda x: ("", 0)),
                     ingest_factory=lambda asr, dev: NS(stored=0, family=False, feed=lambda s: None, flush=lambda: None, wait_idle=lambda: None))

    class Dead:
        def write(self, b):
            if b == b"ok\n":
                raise BrokenPipeError
        def flush(self):
            pass
    h = remote.Handler.__new__(remote.Handler)
    h.server = NS(hub=hub)
    h.rfile = NS(readline=lambda n: b'{"device": "vic", "rate": 16000}\n', read1=lambda n: b"")
    h.wfile = Dead()
    h.handle()                                                         # logged, not raised: the server keeps going
    assert hub.status["vic"]["connected"] is False and not isinstance(hub.claim("vic"), str)


def test_24_orphan_part_chunks_are_finalized_for_retention(tmp_path, monkeypatch):
    monkeypatch.setenv("RAMBLR_AUDIO24_DIR", str(tmp_path / "a24"))
    from ramblr import config, audio24
    importlib.reload(config); importlib.reload(audio24)
    d = tmp_path / "a24" / "2026-10-20"
    d.mkdir(parents=True)
    (d / "1792500000000.opus.part").write_bytes(b"OggS")
    audio24.Writer()
    assert [p.name for p in d.iterdir()] == ["1792500000000.opus"] and len(audio24.chunks()) == 1


def test_8_store_file_never_overwrites(tmp_path, monkeypatch):
    import shutil
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    monkeypatch.setenv("RAMBLR_AUDIO24_DIR", str(tmp_path / "a24"))
    from ramblr import config, audio24
    importlib.reload(config); importlib.reload(audio24)
    import soundfile as sf
    src = tmp_path / "in.wav"
    sf.write(src, np.zeros(16000, np.float32), 16000)
    start = time.time() - 60
    a = audio24.store_file(src, start)
    a.write_bytes(b"EARLIER")
    b = audio24.store_file(src, start)
    assert a != b and a.read_bytes() == b"EARLIER" and b.exists()


def test_37_editing_a_rule_refreshes_cached_rules():
    from ramblr import config, db, vocab
    importlib.reload(vocab)
    d = db.connect(config.DB_PATH)
    vocab.add_word(d, "Ramblr", wrong="rambler")
    assert vocab.apply(d, "rambler")[0] == "Ramblr"
    vocab.add_word(d, "Ramblr Pro", wrong="rambler")            # same row, same count/max id: the old cache missed it
    assert vocab.apply(d, "rambler")[0] == "Ramblr Pro"


def test_4_capture_shutdown_flushes_and_drains():
    from ramblr import capture
    importlib.reload(capture)
    calls = []
    q = NS(unfinished_tasks=0)
    cap = capture.Capture.__new__(capture.Capture)
    cap.proc, cap.stopping = None, True
    cap.ing = NS(flush=lambda: calls.append("flush"), q=q)
    cap.a24 = NS(close=lambda: calls.append("close"))
    cap.run = capture.Capture.run.__get__(cap)
    cap.reconcile = lambda force=False: None
    cap.run()
    assert calls == ["flush", "close"]
