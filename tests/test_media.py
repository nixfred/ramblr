"""B2-1 media detection: decisions from window scores, talk-over by known voices, corrections, exclusions."""
import json
import sqlite3
from datetime import datetime

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RAMBLR_DB", str(tmp_path / "t.db"))
    import importlib
    from ramblr import config
    importlib.reload(config)
    from ramblr import db, media, ideas, nightly, speakers
    for m in (db, media, ideas, nightly, speakers):
        importlib.reload(m)
    return {"db": db.connect(), "dbm": db, "media": media, "ideas": ideas, "nightly": nightly}


T = datetime(2026, 10, 23, 20, 0).timestamp()


def window(env, t0, music=0.0, tv=0.0, speech=0.5, source=None):
    env["db"].execute("INSERT INTO media_windows(ts0,ts1,music,tv,crowd,narration,speech,top,rms,source) VALUES(?,?,?,?,0,0,?,0,0.05,?)",
                      (t0, t0 + 2.88, music, tv, speech, source))


def line(env, t, text, speaker=None, cluster=None):
    sid, _ = env["dbm"].insert_segment(env["db"], t, 2.0, text, None, "t", 1)
    env["db"].execute("UPDATE segments SET speaker_id=?, cluster_id=?, diar=1 WHERE id=?", (speaker, cluster, sid))
    return sid


def media_of(env, sid):
    return env["db"].execute("SELECT media FROM segments WHERE id=?", (sid,)).fetchone()[0]


def test_tv_line_unknown_voice_is_media_known_voice_is_talk(env):
    d, m = env["db"], env["media"]
    fred = env["dbm"].new_speaker(d, "Fred", 1)
    anon = env["dbm"].new_speaker(d)
    for i in range(10):
        window(env, T + i * 2.88, tv=0.8)
    tv_line = line(env, T + 3, "And now the weather for Atlanta.", anon, anon)
    fred_line = line(env, T + 9, "Turn that down, I'm on a call.", fred, fred)
    quiet = line(env, T + 100, "Okay, back to the build.", fred, fred)          # no windows near it
    m.tag_pending(d, until=T + 200)
    assert media_of(env, tv_line) == 1
    assert media_of(env, fred_line) == 0                                       # talk-over: kept
    assert media_of(env, quiet) == 0


def test_retag_after_diarization_names_the_voice(env):
    d, m = env["db"], env["media"]
    anon = env["dbm"].new_speaker(d)
    window(env, T, music=0.9)
    sid = line(env, T + 0.5, "la la la", anon, anon)
    m.tag_pending(d, until=T + 10)
    assert media_of(env, sid) == 1
    d.execute("UPDATE speakers SET name='Fred', named=1 WHERE id=?", (anon,))   # Fred names that voice
    assert m.retag(d, [sid]) == [sid] and media_of(env, sid) == 0


def test_correction_is_final_and_trains(env):
    d, m = env["db"], env["media"]
    anon = env["dbm"].new_speaker(d)
    window(env, T, tv=0.15)
    a = line(env, T + 0.5, "a quiet tv line", anon, anon)
    m.tag_pending(d, until=T + 10)
    assert media_of(env, a) == 0
    before = m.load_model(d)["w"]["tv"]
    out = m.correct(d, [a], True)
    assert media_of(env, a) == 2 and out["corrections"] == 1
    assert m.load_model(d)["w"]["tv"] > before                                 # the correction moved the weight
    assert m.retag(d, [a]) == [] and media_of(env, a) == 2                     # manual flags are never re-decided
    m.correct(d, [a], False)
    assert media_of(env, a) == -1


def test_media_lines_skip_ideas_and_nightly(env):
    d = env["db"]
    fred = env["dbm"].new_speaker(d, "Fred", 1)           # B4-5: only Fred's voice wakes "Larry"
    a = line(env, T, "Larry, I have an idea about a dog collar.", fred, fred)
    b = line(env, T + 5, "Larry, I have an idea, said the TV host.", fred, fred)
    d.execute("UPDATE segments SET media=0 WHERE id=?", (a,))
    d.execute("UPDATE segments SET media=1 WHERE id=?", (b,))
    env["ideas"].scan(d, now=T + 10)
    keys = [r[0] for r in d.execute("SELECT key FROM ideas")]
    assert keys == [f"trigger:{a}"]
    assert [x["id"] for x in env["nightly"].day_lines(d, "2026-10-23")] == [a]
    assert d.execute("SELECT text FROM segments WHERE id=?", (b,)).fetchone()[0]   # the text is kept


def test_blocks_and_never_delete(env):
    rows = [{"id": 1, "ts": 0, "dur": 2, "media": False, "noise": False}, {"id": 2, "ts": 10, "dur": 2, "media": True, "noise": False},
            {"id": 3, "ts": 20, "dur": 2, "media": True, "noise": False}, {"id": 4, "ts": 30, "dur": 2, "media": False, "noise": False},
            {"id": 5, "ts": 40, "dur": 2, "media": True, "noise": False}]
    bl = env["media"].blocks(rows)
    assert [b["ids"] for b in bl] == [[2, 3], [5]]
    window(env, T, music=1)
    with pytest.raises(sqlite3.IntegrityError):
        env["db"].execute("DELETE FROM media_windows")


def test_player_plays_extend_and_close(env, monkeypatch):
    from ramblr import players
    d = env["db"]
    now = [T]
    playing = [{"player": "OmaSpotify", "title": "Song A", "artist": "X", "album": "", "url": "", "status": "Playing"}]

    class Fake(players.Source):
        name = "fake"
        def configured(self): return True
        def poll(self): return list(playing)
    monkeypatch.setattr(players, "SOURCES", [Fake()])
    players.poll_all(d, now=T)
    players.poll_all(d, now=T + 30)
    assert d.execute("SELECT COUNT(*), MAX(last_seen) FROM media_plays").fetchone()[:] == (1, T + 30)
    playing.clear()
    players.poll_all(d, now=T + 200)
    assert d.execute("SELECT ended FROM media_plays").fetchone()[0] == T + 30


def test_sessions_merge_windows(env):
    for i in range(20):
        window(env, T + i * 2.88, music=0.9, speech=0.05)
    for i in range(20):
        window(env, T + 300 + i * 2.88, tv=0.8)
    s = env["media"].sessions(env["db"], T - 10, T + 1000)
    assert [x["kind"] for x in s] == ["music", "tv"] and s[0]["t1"] - s[0]["t0"] > 50


def test_musicid_stub_without_key(env, monkeypatch):
    from ramblr import musicid
    monkeypatch.setattr(musicid, "fpcalc", lambda: None)
    assert musicid.status().startswith("blocked") and musicid.fingerprint(__import__("numpy").zeros(16000 * 20)) is None
    assert musicid.lookup_pending(env["db"]) == 0


def test_song_naming_stays_stubbed_and_a_rejected_key_keeps_tracks_pending(env, monkeypatch):
    import io
    import urllib.error
    from ramblr import config, musicid
    monkeypatch.setattr(musicid, "fpcalc", lambda: "/usr/bin/fpcalc")
    monkeypatch.setattr(config, "ACOUSTID_KEY", "dummy-key-for-test")
    monkeypatch.setattr(musicid, "_rejected", {"at": 0.0, "why": ""})
    d = env["db"]
    d.execute("INSERT INTO media_tracks(ts0,ts1,fingerprint,duration,created) VALUES(1,2,'AQAA',20,0)")
    monkeypatch.setattr(config, "MUSIC_LOOKUP", False)              # Fred 6:55 AM: keep it stubbed
    assert musicid.status().startswith("stub: song naming off") and musicid.lookup_pending(d) == 0

    def bad_key(*a, **k):
        raise urllib.error.HTTPError(musicid.API, 400, "Bad Request", {},
                                     io.BytesIO(b'{"status": "error", "error": {"code": 4, "message": "invalid API key"}}'))
    monkeypatch.setattr(config, "MUSIC_LOOKUP", True)
    monkeypatch.setattr(musicid.urllib.request, "urlopen", bad_key)
    assert musicid.status() == "ready"
    musicid.lookup_pending(d)
    assert d.execute("SELECT status FROM media_tracks").fetchone()[0] == "pending"
    assert "invalid API key" in musicid.status() and musicid.lookup_pending(d) == 0


def test_real_fpcalc_fingerprints_and_cleans_spool(env):
    import shutil
    from pathlib import Path
    import soundfile as sf
    from ramblr import config, musicid
    wav = Path.home() / ".local/share/ramblr/test/jfk16.wav"
    if not shutil.which("fpcalc") or not wav.exists():
        pytest.skip("needs fpcalc and the sample clip")
    fp, dur = musicid.fingerprint(sf.read(str(wav), dtype="float32")[0])
    assert len(fp) > 50 and 10 < dur < 12
    assert not list(config.SPOOL.glob("fp-*.wav"))


def _fake_cast(name, app, state, title=None, series=None, artist=None, season=None, episode=None, idle=False):
    from types import SimpleNamespace as NS
    ms = NS(player_state=state, title=title, series_title=series, artist=artist, album_name=None, season=season, episode=episode)
    return NS(status=NS(display_name=app), media_controller=NS(status=ms), is_idle=idle,
              cast_info=NS(friendly_name=name, model_name="Google TV Streamer", host="10.0.0.9"))


def test_cast_source_reads_now_playing_only(env, monkeypatch):
    from ramblr import players
    importlib = __import__("importlib")
    importlib.reload(players)
    c = players.Cast()
    monkeypatch.setattr(c, "_start", lambda: None)
    c._casts = {"a": _fake_cast("Living Room TV", "Netflix", "PLAYING", title="Pilot", series="Severance", season=1, episode=1),
                "b": _fake_cast("Bedroom TV", "YouTube", "PAUSED", title="x"),
                "c": _fake_cast("Den", "Backdrop", "UNKNOWN", idle=True),
                "d": _fake_cast("Kitchen", "Spotify", "BUFFERING")}
    got = sorted(c.poll(), key=lambda x: x["player"])
    assert [g["player"] for g in got] == ["Kitchen · Spotify", "Living Room TV · Netflix"]
    assert got[1] == {"player": "Living Room TV · Netflix", "title": "Pilot", "artist": "Severance", "album": "S1E1", "url": "", "status": "Playing"}
    assert got[0]["title"] == "(Spotify)"                       # an app without metadata is still logged


def test_cast_play_feeds_detection_but_never_tags_alone(env, monkeypatch):
    d, media = env["db"], env["media"]
    from ramblr import players
    t = 1792760000
    d.execute("INSERT INTO media_plays(source,player,title,artist,album,url,started,last_seen) VALUES('cast','Living Room TV · Netflix','Pilot','','','',?,?)",
              (t, t + 600))
    seg = {"id": 0, "ts": t + 100, "dur": 3.0, "speaker_id": None, "cluster_id": None}
    m, p, f = media.decide(d, seg)
    assert f["cast"] == 1.0 and m == 0 and p < 0.5               # TV on, no acoustic evidence: not tagged
    f2 = dict(f, tv=0.15, unknown=1.0)
    assert media.prob(media.load_model(d), f2) >= 0.5 > media.prob(media.load_model(d), dict(f2, cast=0.0))
    monkeypatch.setenv("RAMBLR_CAST_ROOM", "Office TV")
    assert media.features(d, seg)["cast"] == 0.0                 # a TV in another room does not count
    assert players.cast_playing(d, t + 2000, t + 2003) is False
    old = {"w": {k: v for k, v in media.PRIOR.items() if k != "cast"}, "n": 3}
    assert 0 < media.prob(old, {"music": 0.2}) < 1               # models fitted before the feature still work
    d.execute("INSERT INTO media_corrections(segment_id,label,features,who,created) VALUES(1,1,?,'t',0)", (json.dumps({k: 0.1 for k in media.FEATURES if k != "cast"}),))
    assert "cast" in media.fit(d)["w"]


def test_music_vs_tv_labels_fred_808(env):
    """Fred 8:08 AM: a spoken PC hardware video on vic was labelled "Music playing". Music only for an identified
    song or truly-music audio; narrated media is TV; each mic is labelled from its own windows."""
    d, media = env["db"], env["media"]
    for i in range(10):                                   # vic: narration over a music bed
        window(env, T + i * 2.88, music=0.7, speech=0.8, source="mic:vic")
        window(env, T + i * 2.88, music=0.95, speech=0.02)                 # the room happens to play music meanwhile
    assert media.label_for(d, T, T + 28, "mic:vic") == ("TV", None)
    assert media.label_for(d, T, T + 28, None)[0] == "Music"
    d.execute("INSERT INTO media_tracks(ts0,ts1,fingerprint,duration,created,status,title,artist,source) VALUES(?,?,'x',20,0,'found','Sneaky Snitch','Kevin MacLeod','mic:vic')",
              (T + 5, T + 25))
    assert media.label_for(d, T, T + 28, "mic:vic") == ("Music", "Sneaky Snitch by Kevin MacLeod")   # an identified song is music
    d.execute("INSERT INTO media_plays(source,player,title,artist,album,url,started,last_seen) VALUES('cast','Living Room TV · YouTube','PC build guide','','','',?,?)",
              (T + 400, T + 500))
    for i in range(5):
        window(env, T + 400 + i * 2.88, music=0.6, speech=0.1)
    assert media.label_for(d, T + 400, T + 415, None) == ("TV", "PC build guide")     # Cast says what is on
    s = media.sessions(d, T - 10, T + 100, source="mic:vic")
    assert [x["kind"] for x in s] == ["tv"]


def test_continuous_broadcast_detected_on_its_own_and_stretch_followed_per_source(env):
    """Fred 8:03/8:06 AM: detect TV on every mic by itself (no hand-marking), and follow a TV stretch per source
    until a gap or a real voice. A narrated video runs as back-to-back ~25 s lines; people pause."""
    d, dbm, media = env["db"], env["dbm"], env["media"]
    ids = []
    for k in range(8):                                     # vic: 8 x 25 s of continuous narration, unknown voice
        sid, _ = dbm.insert_segment(d, T + k * 26, 25.0, "the motherboard has four slots", None, "t", 1, source="mic:vic")
        ids.append(sid)
    for k in range(4):                                     # the room: sparse short human lines at the same time
        dbm.insert_segment(d, T + k * 60, 2.0, "want coffee?", None, "t", 1)
    d.execute("UPDATE segments SET media=NULL")
    media.tag_pending(d, until=T + 400, source="mic:vic")
    media.tag_pending(d, until=T + 400)
    vic = d.execute("SELECT media, media_p FROM segments WHERE source='mic:vic' ORDER BY ts").fetchall()
    room = d.execute("SELECT media FROM segments WHERE source IS NULL").fetchall()
    assert all(r["media"] == 1 for r in vic[4:]) and any(r["media_p"] >= 0.5 for r in vic[3:])   # by the model itself
    assert all(r["media"] == 0 for r in room)
    # follow: a short line 20 s after the stretch is still the TV; after a 90 s gap it is not
    a, _ = dbm.insert_segment(d, T + 8 * 26 + 20, 2.0, "and that is the GPU", None, "t", 1, source="mic:vic")
    b, _ = dbm.insert_segment(d, T + 8 * 26 + 200, 2.0, "hey are you hungry", None, "t", 1, source="mic:vic")
    d.execute("UPDATE segments SET media=NULL WHERE id IN (?,?)", (a, b))
    media.tag_pending(d, until=T + 1000, source="mic:vic")
    got = dict(d.execute("SELECT id, media FROM segments WHERE id IN (?,?)", (a, b)).fetchall())
    assert got[a] == 1 and got[b] == 0
    fred = dbm.new_speaker(d, "Fred", 1)                   # a confident known voice right after breaks the stretch
    c, _ = dbm.insert_segment(d, T + 8 * 26 + 25, 2.0, "Larry, mute", None, "t", 1, source="mic:vic")
    d.execute("UPDATE segments SET media=NULL, speaker_id=? WHERE id=?", (fred, c))
    media.tag_pending(d, until=T + 1000, source="mic:vic")
    assert d.execute("SELECT media FROM segments WHERE id=?", (c,)).fetchone()[0] == 0


def test_latest_correction_per_line_wins(env):
    d, dbm, media = env["db"], env["dbm"], env["media"]
    sid, _ = dbm.insert_segment(d, T, 3.0, "x", None, "t", 1)
    media.correct(d, [sid], False)
    media.correct(d, [sid], True)                      # Fred changed his mind: TV
    assert media.fit(d)["n"] == 1                       # one label for the line, the latest


def test_marking_tv_spreads_to_the_stretch_and_undoes_as_one_batch(env):
    """E2b (Fred 8:00 AM): mark one line as TV -> the auto lines around it on the same mic follow, until a gap,
    a different-looking line, a known voice or one of Fred's own decisions. One undoable batch."""
    d, dbm, media = env["db"], env["dbm"], env["media"]
    def ln(t, text, dur=4.0, cluster=7, spk=None):
        sid, _ = dbm.insert_segment(d, t, dur, text, None, "t", 1)
        d.execute("UPDATE segments SET media=0, cluster_id=?, speaker_id=? WHERE id=?", (cluster, spk, sid))
        return sid
    for i in range(60):                                     # a quiet news broadcast: weak TV scores (p ~0.35 band)
        window(env, T - 100 + i * 2.88, tv=0.12, speech=0.8)
    news = [ln(T + k * 10, f"news {k}") for k in range(8)]  # 10 s apart
    after_gap = ln(T + 8 * 10 + 90, "later news")           # 90 s gap: a new stretch
    fred = dbm.new_speaker(d, "Fred", 1)
    talk_before = ln(T - 12, "Fred talking", cluster=fred, spk=fred)
    walled = ln(T + 8 * 10 + 1, "Fred marked this as talk")
    d.execute("UPDATE segments SET media=-1 WHERE id=?", (walled,))
    r = media.correct(d, [news[3]], True)
    assert sorted(r["spread"]) == sorted(news[:3] + news[4:]) and r["batch"]
    got = dict(d.execute("SELECT id, media FROM segments").fetchall())
    assert got[after_gap] == 0 and got[talk_before] == 0 and got[walled] == -1
    old = {a["segment_id"]: json.loads(a["detail"])["old"] for a in d.execute("SELECT segment_id, detail FROM audit WHERE batch_id=?", (r["batch"],))}
    back = media.undo_spread(d, r["batch"])
    got = dict(d.execute("SELECT id, media FROM segments").fetchall())
    assert sorted(back) == sorted(r["spread"]) and all(got[i] == old[i] != 2 for i in r["spread"]) and got[news[3]] == 2
    assert d.execute("SELECT COUNT(*) FROM audit WHERE batch_id=? AND undone_at IS NOT NULL", (r["batch"],)).fetchone()[0] == len(back)


def test_spread_follows_the_cast_window_across_voice_changes(env):
    d, dbm, media = env["db"], env["dbm"], env["media"]
    d.execute("INSERT INTO media_plays(source,player,title,artist,album,url,started,last_seen) VALUES('cast','Living Room TV · YouTube','News','','','',?,?)",
              (T - 30, T + 200))
    for i in range(40):
        window(env, T - 10 + i * 2.88, tv=0.12, speech=0.8)
    ids = []
    for k in range(5):                                      # anchors and reporters: a new voice every line
        sid, _ = dbm.insert_segment(d, T + k * 8, 4.0, f"report {k}", None, "t", 1)
        d.execute("UPDATE segments SET media=0, cluster_id=? WHERE id=?", (100 + k, sid))
        ids.append(sid)
    r = media.correct(d, [ids[0]], True)
    assert sorted(r["spread"]) == sorted(ids[1:])
