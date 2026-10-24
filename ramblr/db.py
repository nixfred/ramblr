"""SQLite (WAL) store. Transcripts are never deleted: a trigger enforces it."""
import os
import sqlite3
import time
from pathlib import Path
from datetime import datetime

import numpy as np

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS speakers(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  named INTEGER NOT NULL DEFAULT 0,      -- 1 once Fred named it: its voiceprint is a saved identity
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS segments(
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,                      -- epoch seconds, start of speech
  dur REAL NOT NULL,
  day TEXT NOT NULL,                     -- local YYYY-MM-DD
  text TEXT NOT NULL,
  noise INTEGER NOT NULL DEFAULT 0,      -- bracket-only output such as [BLANK_AUDIO]; hidden by default, never dropped
  speaker_id INTEGER REFERENCES speakers(id),
  manual INTEGER NOT NULL DEFAULT 0,     -- speaker set by hand, diarization never overrides it
  sim REAL,
  emb_id INTEGER,
  audio TEXT,                            -- tmpfs spool file name until diarized, then NULL
  diar INTEGER NOT NULL DEFAULT 0,       -- 0 pending, 1 done, 2 skipped
  model TEXT,
  asr_ms INTEGER
);
CREATE INDEX IF NOT EXISTS segments_day ON segments(day, ts);
CREATE INDEX IF NOT EXISTS segments_ts ON segments(ts);
CREATE INDEX IF NOT EXISTS segments_pending ON segments(diar, id);
CREATE TABLE IF NOT EXISTS embeddings(
  id INTEGER PRIMARY KEY,
  segment_id INTEGER NOT NULL REFERENCES segments(id),
  dim INTEGER NOT NULL,
  vec BLOB NOT NULL,                     -- float32, L2-normalised
  model TEXT NOT NULL,
  counted_for INTEGER                    -- speaker whose voiceprint sum currently includes this vector
);
CREATE TABLE IF NOT EXISTS voiceprints(
  speaker_id INTEGER PRIMARY KEY REFERENCES speakers(id),
  vec_sum BLOB NOT NULL,                 -- float32 sum of member embeddings; centroid = normalise(sum)
  n INTEGER NOT NULL,
  updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

-- Ideas and their storyboards. Never deleted: Dismiss is only a status; rebuilds add a storyboard VERSION.
CREATE TABLE IF NOT EXISTS ideas(
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,              -- idempotency: trigger:<first segment id> or range:<sha1 of segment ids>
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'new',    -- new | later | done-it | dismissed
  source TEXT NOT NULL,                  -- trigger | range
  open INTEGER NOT NULL DEFAULT 0,       -- 1 while a spoken idea session may still grow
  created REAL NOT NULL,
  updated REAL NOT NULL,
  last_line_ts REAL NOT NULL,
  build_requested INTEGER NOT NULL DEFAULT 0,
  webhook_state TEXT NOT NULL DEFAULT ''  -- '', 'pending webhook', 'sent', 'failed'
);
CREATE TABLE IF NOT EXISTS idea_lines(
  idea_id INTEGER NOT NULL REFERENCES ideas(id),
  segment_id INTEGER NOT NULL REFERENCES segments(id),
  PRIMARY KEY(idea_id, segment_id)
);
CREATE TABLE IF NOT EXISTS storyboards(
  id INTEGER PRIMARY KEY,
  idea_id INTEGER NOT NULL REFERENCES ideas(id),
  version INTEGER NOT NULL,
  status TEXT NOT NULL,                  -- building | ok | error
  created REAL NOT NULL,
  generator TEXT, model TEXT, latency_s REAL, error TEXT,
  body TEXT,                             -- storyboard JSON
  UNIQUE(idea_id, version)
);
CREATE TRIGGER IF NOT EXISTS ideas_never_delete BEFORE DELETE ON ideas
BEGIN SELECT RAISE(ABORT, 'ramblr: ideas are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS storyboards_never_delete BEFORE DELETE ON storyboards
BEGIN SELECT RAISE(ABORT, 'ramblr: storyboards are never deleted'); END;

-- Speaker reassignment audit (who, from, to, when) so every change can be undone. Never deleted.
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY,
  batch_id TEXT NOT NULL,
  parent_batch TEXT,                     -- a propagation batch points at the reassign that caused it
  kind TEXT NOT NULL,                    -- reassign | bulk | propagate | accept | merge | rename | color | confirm
  segment_id INTEGER, from_speaker INTEGER, to_speaker INTEGER, from_manual INTEGER, to_manual INTEGER,
  vp_moved INTEGER NOT NULL DEFAULT 0,   -- 1 if this line's voice embedding was moved between voiceprints
  emb_counted_before INTEGER, emb_weight_before REAL,
  score REAL, detail TEXT, who TEXT NOT NULL DEFAULT 'site',
  created REAL NOT NULL, undone_at REAL
);
CREATE INDEX IF NOT EXISTS audit_batch ON audit(batch_id);
CREATE INDEX IF NOT EXISTS audit_parent ON audit(parent_batch);
CREATE TABLE IF NOT EXISTS suggestions(  -- low-confidence propagation matches waiting for a one-click confirm
  id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, segment_id INTEGER NOT NULL, person_id INTEGER NOT NULL,
  score REAL NOT NULL, same_cluster INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'open',    -- open | accepted | rejected | void
  created REAL NOT NULL, decided REAL
);
CREATE TABLE IF NOT EXISTS cluster_map(  -- diarizer cluster (an unnamed voice) -> the person Fred said it is
  cluster_id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, batch_id TEXT, active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL
);
-- Phase 2. Every table below is append/update only; triggers forbid DELETE.
-- bookmarks: feature removed (Fred, 2026-10-24 4:35 AM: "I dont want bookmarks at all"). The table and its rows
-- stay as they are, unused; never dropped, never deleted.
CREATE TABLE IF NOT EXISTS bookmarks(
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,              -- hotkey:<ms> | site:<ms> | spoken:<segment id>  (idempotent)
  ts REAL NOT NULL,                      -- the moment marked
  source TEXT NOT NULL,                  -- hotkey | site | spoken
  segment_id INTEGER,                    -- spoken: the line that said it
  note TEXT NOT NULL DEFAULT '',
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS bookmarks_ts ON bookmarks(ts);
CREATE TABLE IF NOT EXISTS action_items(
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,              -- stable: day + first source line + normalised task
  day TEXT NOT NULL,
  owner TEXT NOT NULL DEFAULT '',
  task TEXT NOT NULL,
  specifics TEXT NOT NULL DEFAULT '',
  due TEXT,                              -- ISO date (YYYY-MM-DD) resolved against the utterance time, or NULL
  due_phrase TEXT NOT NULL DEFAULT '',   -- what was said ('tomorrow', 'by Friday')
  line_ids TEXT NOT NULL,                -- JSON list of source segment ids
  source_ts REAL NOT NULL,               -- time of the first source line
  sent INTEGER NOT NULL DEFAULT 0,
  sent_at REAL,
  created REAL NOT NULL,
  model TEXT
);
CREATE INDEX IF NOT EXISTS action_items_created ON action_items(created);
CREATE TABLE IF NOT EXISTS summaries(
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL,
  version INTEGER NOT NULL,
  created REAL NOT NULL,
  generator TEXT, model TEXT, latency_s REAL,
  lines INTEGER NOT NULL DEFAULT 0,      -- how many transcript lines it covers
  body TEXT NOT NULL,                    -- JSON
  UNIQUE(day, version)
);
CREATE TABLE IF NOT EXISTS nightly_runs(
  id INTEGER PRIMARY KEY, day TEXT NOT NULL, started REAL NOT NULL, finished REAL,
  status TEXT NOT NULL DEFAULT 'running', detail TEXT
);
CREATE TABLE IF NOT EXISTS chunks(       -- ask-your-transcript: consecutive lines embedded together
  id INTEGER PRIMARY KEY,
  first_seg INTEGER NOT NULL UNIQUE, last_seg INTEGER NOT NULL,
  day TEXT NOT NULL, ts0 REAL NOT NULL, ts1 REAL NOT NULL,
  seg_ids TEXT NOT NULL,                 -- JSON list
  vec BLOB NOT NULL, model TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_day ON chunks(day);
CREATE TABLE IF NOT EXISTS idea_briefs(  -- 'Cleaned up' edits; each save is a new version
  id INTEGER PRIMARY KEY, idea_id INTEGER NOT NULL REFERENCES ideas(id), version INTEGER NOT NULL,
  body TEXT NOT NULL, who TEXT NOT NULL DEFAULT 'site', created REAL NOT NULL, UNIQUE(idea_id, version)
);
-- Voice routing (B3-14): "Larry, tell <bot> to <task>" lines, routed to Larry Manager's webhook. Ramblr never
-- executes a command itself.
CREATE TABLE IF NOT EXISTS command_intents(
  id INTEGER PRIMARY KEY, segment_id INTEGER NOT NULL UNIQUE, ts REAL NOT NULL, phrase TEXT NOT NULL,
  intent TEXT, status TEXT NOT NULL DEFAULT 'disabled', created REAL NOT NULL
);
-- B2-1 media detection: classifier scores per 2.88 s window (numbers only, never audio; quiet windows skipped)
CREATE TABLE IF NOT EXISTS media_windows(
  id INTEGER PRIMARY KEY, ts0 REAL NOT NULL, ts1 REAL NOT NULL,
  music REAL, tv REAL, crowd REAL, narration REAL, speech REAL, top INTEGER, rms REAL
);
CREATE INDEX IF NOT EXISTS media_windows_ts ON media_windows(ts1);
CREATE TABLE IF NOT EXISTS media_corrections(   -- Fred's "that was the TV" / "not media" clicks: training data
  id INTEGER PRIMARY KEY, segment_id INTEGER NOT NULL, label INTEGER NOT NULL, features TEXT NOT NULL,
  who TEXT NOT NULL DEFAULT 'site', created REAL NOT NULL
);
-- B2-2 media log: what the players said was playing, and music fingerprints (never audio)
CREATE TABLE IF NOT EXISTS media_plays(
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, player TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', artist TEXT NOT NULL DEFAULT '',
  album TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', started REAL NOT NULL, last_seen REAL NOT NULL, ended REAL
);
CREATE INDEX IF NOT EXISTS media_plays_started ON media_plays(started);
CREATE TABLE IF NOT EXISTS media_tracks(
  id INTEGER PRIMARY KEY, ts0 REAL NOT NULL, ts1 REAL NOT NULL, fingerprint TEXT NOT NULL, duration REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',    -- pending | found | unknown | error
  title TEXT, artist TEXT, acoustid TEXT, score REAL, error TEXT, created REAL NOT NULL
);
-- B3-0 iPhone ramble uploads. The audio file itself follows the audio rule (tmpfs, removed after transcription,
-- or the 24h store once B3-15 is live); this row and the transcript are kept forever.
CREATE TABLE IF NOT EXISTS uploads(
  id INTEGER PRIMARY KEY, source TEXT NOT NULL DEFAULT 'iPhone', name TEXT, bytes INTEGER NOT NULL, mime TEXT,
  status TEXT NOT NULL DEFAULT 'queued',     -- queued | working | done | error
  recorded_at REAL, duration REAL, segments INTEGER, idea_id INTEGER, error TEXT, who TEXT,
  created REAL NOT NULL, finished REAL, file TEXT
);
-- B3-7 conversations and their end-of-conversation summary cards (saved forever)
CREATE TABLE IF NOT EXISTS conversations(
  id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, ts0 REAL NOT NULL, ts1 REAL NOT NULL, first_seg INTEGER NOT NULL,
  last_seg INTEGER NOT NULL, lines INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'open', people TEXT,
  card TEXT, model TEXT, card_tries INTEGER NOT NULL DEFAULT 0, card_error TEXT, created REAL NOT NULL, closed_at REAL
);
CREATE INDEX IF NOT EXISTS conversations_ts ON conversations(ts0);
-- B3-1 vocabulary: Fred's text fixes (originals kept forever) and the words/pairs learned from them
CREATE TABLE IF NOT EXISTS text_edits(
  id INTEGER PRIMARY KEY, segment_id INTEGER NOT NULL, old TEXT NOT NULL, new TEXT NOT NULL, who TEXT, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS vocab(
  id INTEGER PRIMARY KEY, wrong TEXT NOT NULL UNIQUE, right TEXT NOT NULL, source TEXT NOT NULL, hits INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS text_edits_never_delete BEFORE DELETE ON text_edits
BEGIN SELECT RAISE(ABORT, 'ramblr: text edits are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS vocab_never_delete BEFORE DELETE ON vocab
BEGIN SELECT RAISE(ABORT, 'ramblr: vocabulary entries are switched off, never deleted'); END;
-- B3-8 facts, B3-9 goals + weekly pattern insights, B3-10 topic trackers. Statuses change; rows never go.
CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, subject TEXT NOT NULL, fact TEXT NOT NULL, kind TEXT,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, mentions INTEGER NOT NULL DEFAULT 1, line_ids TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', superseded_by INTEGER, vec BLOB, model TEXT, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS goals(
  id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, owner TEXT, goal TEXT NOT NULL, by_phrase TEXT, first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL, mentions INTEGER NOT NULL DEFAULT 1, line_ids TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS insights(id INTEGER PRIMARY KEY, week TEXT NOT NULL, body TEXT NOT NULL, numbers TEXT, model TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS topic_trackers(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, query TEXT NOT NULL, phrase INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS facts_never_delete BEFORE DELETE ON facts BEGIN SELECT RAISE(ABORT, 'ramblr: facts are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS goals_never_delete BEFORE DELETE ON goals BEGIN SELECT RAISE(ABORT, 'ramblr: goals are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS insights_never_delete BEFORE DELETE ON insights BEGIN SELECT RAISE(ABORT, 'ramblr: insights are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS topic_trackers_never_delete BEFORE DELETE ON topic_trackers BEGIN SELECT RAISE(ABORT, 'ramblr: trackers are switched off, never deleted'); END;
-- B3-11 auto-sorted folders
CREATE TABLE IF NOT EXISTS folders(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS folder_items(item_type TEXT NOT NULL, item_id INTEGER NOT NULL, folder_id INTEGER NOT NULL,
  source TEXT NOT NULL DEFAULT 'auto', created REAL NOT NULL, PRIMARY KEY(item_type, item_id));
CREATE TRIGGER IF NOT EXISTS folders_never_delete BEFORE DELETE ON folders BEGIN SELECT RAISE(ABORT, 'ramblr: folders are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS folder_items_never_delete BEFORE DELETE ON folder_items BEGIN SELECT RAISE(ABORT, 'ramblr: filing is moved, never deleted'); END;
-- B3-12 event outbox + webhook deliveries, plugin runs (kept forever)
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries(event_id INTEGER NOT NULL, hook INTEGER NOT NULL, status TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0,
  error TEXT, next_try REAL NOT NULL DEFAULT 0, sent_at REAL, created REAL NOT NULL, PRIMARY KEY(event_id, hook));
CREATE TABLE IF NOT EXISTS plugin_runs(id INTEGER PRIMARY KEY, plugin TEXT NOT NULL, slot TEXT NOT NULL, status TEXT NOT NULL, output TEXT,
  model TEXT, lines INTEGER, created REAL NOT NULL, finished REAL, UNIQUE(plugin, slot));
CREATE TRIGGER IF NOT EXISTS events_never_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'ramblr: events are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS deliveries_never_delete BEFORE DELETE ON deliveries BEGIN SELECT RAISE(ABORT, 'ramblr: deliveries are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS plugin_runs_never_delete BEFORE DELETE ON plugin_runs BEGIN SELECT RAISE(ABORT, 'ramblr: plugin runs are never deleted'); END;
-- B3-13 follow-up drafts (never sent by Ramblr) and their edits
CREATE TABLE IF NOT EXISTS drafts(id INTEGER PRIMARY KEY, conversation_id INTEGER, kind TEXT NOT NULL, recipient TEXT, subject TEXT,
  body TEXT NOT NULL, why TEXT, status TEXT NOT NULL DEFAULT 'draft', model TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS draft_edits(id INTEGER PRIMARY KEY, draft_id INTEGER NOT NULL, old TEXT, new TEXT, who TEXT, created REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS drafts_never_delete BEFORE DELETE ON drafts BEGIN SELECT RAISE(ABORT, 'ramblr: drafts are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS draft_edits_never_delete BEFORE DELETE ON draft_edits BEGIN SELECT RAISE(ABORT, 'ramblr: draft edits are never deleted'); END;
-- B4-1 imports (Zoom transcripts, Wispr dictations): one row per file and per line, never duplicated or deleted
CREATE TABLE IF NOT EXISTS imports(hash TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT, result TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS import_lines(key TEXT PRIMARY KEY, segment_id INTEGER NOT NULL, created REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS imports_never_delete BEFORE DELETE ON imports BEGIN SELECT RAISE(ABORT, 'ramblr: imports are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS import_lines_never_delete BEFORE DELETE ON import_lines BEGIN SELECT RAISE(ABORT, 'ramblr: imports are never deleted'); END;
-- B4-8 idea lineage: the same pitch coming back
CREATE TABLE IF NOT EXISTS idea_links(a INTEGER NOT NULL, b INTEGER NOT NULL, score REAL NOT NULL, created REAL NOT NULL, PRIMARY KEY(a, b));
CREATE TRIGGER IF NOT EXISTS idea_links_never_delete BEFORE DELETE ON idea_links BEGIN SELECT RAISE(ABORT, 'ramblr: idea links are never deleted'); END;
-- B4-6 calendar cache (read-only feed) and B4-13 ambient context snapshots
CREATE TABLE IF NOT EXISTS calendar_events(uid TEXT PRIMARY KEY, start REAL NOT NULL, end REAL NOT NULL, all_day INTEGER NOT NULL DEFAULT 0,
  title TEXT, location TEXT, status TEXT, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS calendar_events_start ON calendar_events(start);
CREATE TABLE IF NOT EXISTS ambient(id INTEGER PRIMARY KEY, ts REAL NOT NULL, window_class TEXT, window_title TEXT, event TEXT, weather TEXT);
CREATE INDEX IF NOT EXISTS ambient_ts ON ambient(ts);
CREATE TRIGGER IF NOT EXISTS calendar_events_never_delete BEFORE DELETE ON calendar_events BEGIN SELECT RAISE(ABORT, 'ramblr: calendar cache rows are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS ambient_never_delete BEFORE DELETE ON ambient BEGIN SELECT RAISE(ABORT, 'ramblr: context tags are never deleted'); END;
-- B4-11 quote pulls (verbatim only)
CREATE TABLE IF NOT EXISTS quote_pulls(id INTEGER PRIMARY KEY, title TEXT, topic TEXT, start REAL, end REAL, quotes TEXT NOT NULL, model TEXT, created REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS quote_pulls_never_delete BEFORE DELETE ON quote_pulls BEGIN SELECT RAISE(ABORT, 'ramblr: quote pulls are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS conversations_never_delete BEFORE DELETE ON conversations
BEGIN SELECT RAISE(ABORT, 'ramblr: conversation cards are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS uploads_never_delete BEFORE DELETE ON uploads
BEGIN SELECT RAISE(ABORT, 'ramblr: upload records are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS media_plays_never_delete BEFORE DELETE ON media_plays
BEGIN SELECT RAISE(ABORT, 'ramblr: the media log is never deleted'); END;
CREATE TRIGGER IF NOT EXISTS media_tracks_never_delete BEFORE DELETE ON media_tracks
BEGIN SELECT RAISE(ABORT, 'ramblr: the media log is never deleted'); END;
CREATE TRIGGER IF NOT EXISTS media_windows_never_delete BEFORE DELETE ON media_windows
BEGIN SELECT RAISE(ABORT, 'ramblr: media tags are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS media_corrections_never_delete BEFORE DELETE ON media_corrections
BEGIN SELECT RAISE(ABORT, 'ramblr: media corrections are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS bookmarks_never_delete BEFORE DELETE ON bookmarks
BEGIN SELECT RAISE(ABORT, 'ramblr: bookmarks are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS action_items_never_delete BEFORE DELETE ON action_items
BEGIN SELECT RAISE(ABORT, 'ramblr: action items are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS summaries_never_delete BEFORE DELETE ON summaries
BEGIN SELECT RAISE(ABORT, 'ramblr: summaries are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS idea_briefs_never_delete BEFORE DELETE ON idea_briefs
BEGIN SELECT RAISE(ABORT, 'ramblr: idea briefs are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS command_intents_never_delete BEFORE DELETE ON command_intents
BEGIN SELECT RAISE(ABORT, 'ramblr: command intents are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS audit_never_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'ramblr: the audit trail is never deleted'); END;
CREATE TRIGGER IF NOT EXISTS segments_never_delete BEFORE DELETE ON segments
BEGIN SELECT RAISE(ABORT, 'ramblr: transcripts are never deleted'); END;
"""


LIVE_DB = config.HOME / ".local/share/ramblr/ears.db"


def connect(path=None):
    path = path or config.DB_PATH
    if os.environ.get("PYTEST_CURRENT_TEST") and Path(path).resolve() == LIVE_DB.resolve():
        raise RuntimeError("tests must never open the live DB; use a tmp path (RAMBLR_DB)")
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    migrate(db)
    return db


def migrate(db):
    """Additive, idempotent column migrations (safe when several services connect at once)."""
    adds = [("speakers", "color", "TEXT"), ("speakers", "confirmed", "INTEGER NOT NULL DEFAULT 0"),
            ("speakers", "merged_into", "INTEGER"), ("segments", "cluster_id", "INTEGER"),
            ("embeddings", "weight", "REAL NOT NULL DEFAULT 1"),
            ("segments", "media", "INTEGER"), ("segments", "media_p", "REAL"),
            ("bookmarks", "span_start", "REAL"), ("bookmarks", "span_end", "REAL"), ("segments", "source", "TEXT"),
            ("segments", "text_orig", "TEXT"),
            ("command_intents", "command_id", "TEXT"), ("command_intents", "target", "TEXT"), ("command_intents", "task", "TEXT"),
            ("command_intents", "speaker", "TEXT"), ("command_intents", "confidence", "REAL"), ("command_intents", "result", "TEXT"),
            ("command_intents", "tries", "INTEGER"), ("command_intents", "sent_at", "REAL"), ("command_intents", "next_try", "REAL"),
            ("ideas", "vec", "BLOB"), ("ideas", "vec_rev", "INTEGER"), ("ideas", "heat", "REAL"), ("ideas", "mentions", "INTEGER"),
            ("ideas", "last_mention", "REAL"), ("ideas", "repo_url", "TEXT"), ("ideas", "shipped_at", "REAL"),
            ("action_items", "done_at", "REAL"), ("uploads", "attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("media_windows", "source", "TEXT"), ("media_tracks", "source", "TEXT"),
            ("command_intents", "voice_hold", "INTEGER NOT NULL DEFAULT 0"), ("command_intents", "fred_approved", "INTEGER NOT NULL DEFAULT 0")]
    for table, col, decl in adds:
        if col not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
            try:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise
    # the diarizer's own decision is the line's cluster; backfill it once for lines labelled before clusters existed
    db.execute("UPDATE segments SET cluster_id=speaker_id WHERE cluster_id IS NULL AND diar=1 AND manual=0 AND speaker_id IS NOT NULL")
    db.execute("CREATE INDEX IF NOT EXISTS segments_media ON segments(media)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS command_intents_cid ON command_intents(command_id) WHERE command_id IS NOT NULL")
    # B3-3 word index (FTS5, external content: the text lives only in segments). Index maintenance only;
    # transcript rows are never deleted.
    db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(text, content='segments', content_rowid='id',
                  tokenize='porter unicode61')""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS segments_fts_ai AFTER INSERT ON segments
                  BEGIN INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS segments_fts_au AFTER UPDATE OF text ON segments
                  BEGIN INSERT INTO segments_fts(segments_fts, rowid, text) VALUES('delete', old.id, old.text);
                        INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text); END""")
    if get_meta(db, "fts_built") != "1":
        db.execute("INSERT INTO segments_fts(segments_fts) VALUES('rebuild')")
        set_meta(db, "fts_built", "1")


def bump_rev(db):
    db.execute("INSERT INTO meta(key,value) VALUES('rev','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")


def set_meta(db, key, value):
    db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def get_meta(db, key, default=None):
    r = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def is_noise(text):
    t = text.strip()
    return (not any(c.isalnum() for c in t)) or (t[:1] in "[(*♪" and t[-1:] in "])*♪")


def insert_segment(db, ts, dur, text, audio, model, asr_ms, source=None):
    """source: None = the room mic; 'iPhone' = an uploaded ramble (B3-0); later imports name themselves."""
    day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    noise = 1 if is_noise(text) else 0
    diar = 0 if (audio and not noise) else 2
    cur = db.execute(
        "INSERT INTO segments(ts,dur,day,text,noise,audio,diar,model,asr_ms,source,media) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (ts, dur, day, text, noise, audio if diar == 0 else None, diar, model, asr_ms, source,
         0 if source else None))      # an uploaded ramble is never TV: decided as talk at once
    return cur.lastrowid, diar


def new_speaker(db, name=None, named=0):
    if name is None:
        n = int(get_meta(db, "speaker_counter", "0")) + 1
        set_meta(db, "speaker_counter", n)
        name = f"Speaker {n}"
    cur = db.execute("INSERT INTO speakers(name,named,created) VALUES(?,?,?)", (name, named, time.time()))
    return cur.lastrowid


def vec_to_blob(v):
    return np.asarray(v, dtype=np.float32).tobytes()


def blob_to_vec(b):
    return np.frombuffer(b, dtype=np.float32).copy()


def voiceprints(db):
    """[(speaker_id, centroid)] for every live speaker with a voiceprint (merged speakers excluded)."""
    out = []
    for r in db.execute("SELECT v.speaker_id, v.vec_sum FROM voiceprints v JOIN speakers s ON s.id=v.speaker_id "
                        "WHERE s.merged_into IS NULL"):
        s = blob_to_vec(r["vec_sum"])
        nrm = np.linalg.norm(s)
        if nrm > 0:
            out.append((r["speaker_id"], s / nrm))
    return out


def add_to_voiceprint(db, speaker_id, emb_id, vec):
    """Count one stored embedding into a speaker's voiceprint with its weight (idempotent per embedding).
    The voiceprint is a weighted running sum; its direction is the centroid."""
    row = db.execute("SELECT counted_for, weight FROM embeddings WHERE id=?", (emb_id,)).fetchone()
    if row and row["counted_for"] == speaker_id:
        return
    if row and row["counted_for"] is not None:
        remove_from_voiceprint(db, emb_id, vec)
    w = float(row["weight"]) if row else 1.0
    cur = db.execute("SELECT vec_sum, n FROM voiceprints WHERE speaker_id=?", (speaker_id,)).fetchone()
    if cur:
        s = blob_to_vec(cur["vec_sum"]) + w * vec
        db.execute("UPDATE voiceprints SET vec_sum=?, n=?, updated=? WHERE speaker_id=?",
                   (vec_to_blob(s), cur["n"] + 1, time.time(), speaker_id))
    else:
        db.execute("INSERT INTO voiceprints(speaker_id,vec_sum,n,updated) VALUES(?,?,?,?)",
                   (speaker_id, vec_to_blob(w * vec), 1, time.time()))
    db.execute("UPDATE embeddings SET counted_for=? WHERE id=?", (speaker_id, emb_id))


def remove_from_voiceprint(db, emb_id, vec):
    row = db.execute("SELECT counted_for, weight FROM embeddings WHERE id=?", (emb_id,)).fetchone()
    if not row or row["counted_for"] is None:
        return
    cur = db.execute("SELECT vec_sum, n FROM voiceprints WHERE speaker_id=?", (row["counted_for"],)).fetchone()
    if cur:
        s = blob_to_vec(cur["vec_sum"]) - float(row["weight"]) * vec
        db.execute("UPDATE voiceprints SET vec_sum=?, n=?, updated=? WHERE speaker_id=?",
                   (vec_to_blob(s), max(cur["n"] - 1, 0), time.time(), row["counted_for"]))
    db.execute("UPDATE embeddings SET counted_for=NULL WHERE id=?", (emb_id,))


def recompute_voiceprint(db, speaker_id):
    """Rebuild one voiceprint exactly from the embeddings counted for it (used by undo and merge-undo)."""
    s, n = None, 0
    for r in db.execute("SELECT vec, weight FROM embeddings WHERE counted_for=?", (speaker_id,)):
        v = float(r["weight"]) * blob_to_vec(r["vec"])
        s = v if s is None else s + v
        n += 1
    if s is None:
        db.execute("DELETE FROM voiceprints WHERE speaker_id=?", (speaker_id,))
    else:
        db.execute("INSERT INTO voiceprints(speaker_id,vec_sum,n,updated) VALUES(?,?,?,?) "
                   "ON CONFLICT(speaker_id) DO UPDATE SET vec_sum=excluded.vec_sum, n=excluded.n, updated=excluded.updated",
                   (speaker_id, vec_to_blob(s), n, time.time()))


def merge_speakers(db, src, dst):
    """Fold speaker src into dst (rename to an existing name). Transcript rows are only re-pointed."""
    if src == dst:
        return
    db.execute("UPDATE segments SET speaker_id=? WHERE speaker_id=?", (dst, src))
    a = db.execute("SELECT vec_sum, n FROM voiceprints WHERE speaker_id=?", (src,)).fetchone()
    b = db.execute("SELECT vec_sum, n FROM voiceprints WHERE speaker_id=?", (dst,)).fetchone()
    if a and b:
        s = blob_to_vec(a["vec_sum"]) + blob_to_vec(b["vec_sum"])
        db.execute("UPDATE voiceprints SET vec_sum=?, n=?, updated=? WHERE speaker_id=?",
                   (vec_to_blob(s), a["n"] + b["n"], time.time(), dst))
    elif a:
        db.execute("INSERT INTO voiceprints(speaker_id,vec_sum,n,updated) VALUES(?,?,?,?)",
                   (dst, a["vec_sum"], a["n"], time.time()))
    db.execute("UPDATE embeddings SET counted_for=? WHERE counted_for=?", (dst, src))
    db.execute("DELETE FROM voiceprints WHERE speaker_id=?", (src,))
    db.execute("DELETE FROM speakers WHERE id=?", (src,))
