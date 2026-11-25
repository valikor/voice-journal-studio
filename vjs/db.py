"""SQLite store. One file (data/studio.db) holds recordings, ASR segments, mined
moments, review decisions and the language log."""
import json
import sqlite3
import threading
from contextlib import contextmanager

from .config import DATA

import os
DB_PATH = __import__("pathlib").Path(os.environ["VJS_DB"]) if os.environ.get("VJS_DB") else DATA / "studio.db"  # VJS_DB: test against a copy

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS recordings (
  id TEXT PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  filename TEXT NOT NULL,
  recorded_at TEXT,             -- local ISO datetime
  duration REAL,
  title TEXT,                   -- Voice Memos title (often a place)
  status TEXT NOT NULL DEFAULT 'new',  -- new|transcribed|analyzed|mined|skipped|error
  error TEXT,
  kid_segments INTEGER DEFAULT 0,
  asr_seconds REAL,
  added_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY,
  rec_id TEXT NOT NULL REFERENCES recordings(id),
  idx INTEGER NOT NULL,
  start REAL, end REAL,
  text TEXT,                    -- raw ASR
  lang TEXT,                    -- zh|en|mixed|other
  avg_logprob REAL, no_speech REAL,
  f0_med REAL, f0_p90 REAL, centroid REAL, voiced_frac REAL,
  spk_guess TEXT, spk_conf REAL,
  spk TEXT,                     -- human-confirmed speaker
  text_final TEXT,              -- human-corrected text
  flags TEXT,                   -- e.g. loop
  adult_like INTEGER,           -- 1 = usage check says this is clearly not the kid talking
  UNIQUE(rec_id, idx)
);
CREATE INDEX IF NOT EXISTS seg_rec ON segments(rec_id, idx);
CREATE TABLE IF NOT EXISTS moments (
  id INTEGER PRIMARY KEY,
  rec_id TEXT NOT NULL REFERENCES recordings(id),
  seg_from INTEGER NOT NULL, seg_to INTEGER NOT NULL,   -- segment idx range (inclusive)
  score INTEGER, tags TEXT, why TEXT,
  status TEXT NOT NULL DEFAULT 'pending', -- pending|later|journal|log|rejected|ready|published
  note_public TEXT DEFAULT '',  -- published as *[...]*
  note_private TEXT DEFAULT '', -- never leaves this app
  entry_date TEXT,
  star INTEGER DEFAULT 0,
  triaged_at TEXT, polished_at TEXT, published_at TEXT, publish_path TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime')),
  UNIQUE(rec_id, seg_from, seg_to)
);
CREATE INDEX IF NOT EXISTS mom_status ON moments(status, score);
CREATE TABLE IF NOT EXISTS moment_lines (
  id INTEGER PRIMARY KEY,
  moment_id INTEGER NOT NULL REFERENCES moments(id) ON DELETE CASCADE,
  ord INTEGER NOT NULL,
  seg_id INTEGER,
  speaker TEXT,
  text TEXT,                    -- verbatim quote
  rendered TEXT,                -- house-style line incl. glosses (editable)
  include INTEGER DEFAULT 1,
  alts TEXT                     -- JSON {auto, zh, en}
);
CREATE TABLE IF NOT EXISTS usage_flags (
  id INTEGER PRIMARY KEY,
  seg_id INTEGER NOT NULL REFERENCES segments(id),
  lang TEXT, kind TEXT, span TEXT, correct TEXT, explanation TEXT,
  status TEXT DEFAULT 'suggested',  -- suggested|confirmed|dismissed
  UNIQUE(seg_id, span, kind)
);
CREATE TABLE IF NOT EXISTS usage_checked (seg_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS watch_words (word TEXT PRIMARY KEY, added_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

_local = threading.local()
_schema_lock = threading.Lock()
_schema_done = False


def conn():
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        global _schema_done
        with _schema_lock:
            if not _schema_done:
                have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"recordings", "segments", "moments", "moment_lines", "usage_flags", "kv"} <= have:
                    c.executescript(SCHEMA)
                _migrate(c)
                _schema_done = True
        _local.c = c
    return c


MIGRATIONS = [
    ("moments", "parked_at", "TEXT"),         # small (<=2 line) waiting card moved out of New into "Parked small cards" (vjs/queuecut.py); Quick still draws it
    ("moments", "kid_sim", "REAL"),            # voice-embedding similarity of the card's "kid" lines to her confirmed voice (vjs/mediacard.py); low = maybe TV/another child
    ("moments", "why_hash", "TEXT"),          # hash of the card text the AI note was verified against (vjs/notes.py)
    ("moments", "why_orig", "TEXT"),          # the model's original note when notes.py replaced it
    ("moments", "why_state", "TEXT"),         # ok | extractive | llm
    ("moments", "touched_at", "TEXT"),        # last time the owner edited a line on the card (vjs/edits.py); a pending card with this set is "Started", never "New"
    ("moments", "punch_score", "REAL"),       # v2 pass: LLM rating 1-5 of how likely the owner would publish the card (vjs/punchlines.py)
    ("moments", "punch_v", "INTEGER"),        # which punchline-pass version filled punch_*
    ("recordings", "kid_ok", "INTEGER"),     # 1 = the owner says she can be in this recording, overriding the date/allow-list rule (vjs/rules.py allow_kid)
    ("recordings", "archive", "INTEGER"),    # 1 = the owner keeps it as archive/work/personal: transcribed for the Reader + Search, never mined for moments (vjs/sortrec.py)
    ("usage_flags", "auto", "TEXT"),         # why a flag was auto-dismissed (vjs/usage_filter.py); NULL = a human decision or still suggested
    ("moments", "punch_seg", "INTEGER"),     # her single best line on the card (segments.id) — vjs/punchlines.py
    ("moments", "punch_tag", "TEXT"),        # 3–5 word why ("invented word", "code-switch", "sass"…)
    ("moments", "punch_at", "TEXT"),         # when the pass looked at this card (NULL = not yet; resumable)
    ("recordings", "fp", "TEXT"),            # content fingerprint (size + first/last 256 KB) — survives renames (vjs/storage.py)
    ("moments", "rank", "REAL"),
    ("moments", "learned", "REAL"),          # P(the owner keeps it), vjs/learn.py
    ("moments", "trust_ok", "INTEGER"),      # 0 = picked quote missing / listens disagree / not surely her → "needs ears"
    ("moments", "audio", "TEXT"),            # published clip, site path (vjs/journal_audio.py)
    ("moments", "audio_off", "INTEGER"),
    ("moments", "autoclean", "INTEGER"),     # 1 = safe clean-up applied, -1 = the owner undid it (don't re-apply)     # 1 = the owner switched her voice off for this quote             # overnight tournament: how likely you'd keep it (vjs/rank.py)
    ("segments", "lang_hint", "TEXT"),       # wrong-language scan: {"p_zh", "zh"?} for English lines (vjs/langscan.py)
    ("segments", "adult_like", "INTEGER"),   # usage check: clearly not the kid talking
    ("segments", "asr_lang", "TEXT"),        # whisper's detected language for the chunk
    ("recordings", "category", "TEXT"),      # family|mixed|work|media (from mining)
    ("recordings", "prior", "TEXT"),         # hint from the old review_index summaries
    ("moments", "priority", "INTEGER DEFAULT 1"),
    ("recordings", "vad_v", "INTEGER"),
    ("segments", "src", "TEXT"),                 # 'gap' = inserted by gap-fill (dedupe target)
    ("segments", "q", "REAL"),                   # line quality 0-1 (see quality.py)
    ("moment_lines", "edited", "INTEGER DEFAULT 0"),  # you touched it → rebuilds keep it
    ("moments", "quality", "REAL"),              # quality of her lines in the moment
    ("moments", "relisten", "TEXT"),             # JSON: the moment re-transcribed as one clip          # 2 = transcribed/gap-filled with the sensitive VAD  # 0 family · 1 mixed/unknown · 2 work/media
]


def _migrate(c):
    c.execute("""CREATE TABLE IF NOT EXISTS edits (
        id INTEGER PRIMARY KEY, at TEXT DEFAULT (datetime('now','localtime')),
        kind TEXT, seg_id INTEGER, moment_id INTEGER, before TEXT, after TEXT, via TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS lexicon (
        wrong TEXT, right TEXT, n INTEGER DEFAULT 1, last TEXT, PRIMARY KEY (wrong, right))""")
    c.execute("""CREATE TABLE IF NOT EXISTS spot_checks (
        id INTEGER PRIMARY KEY, seg_id INTEGER NOT NULL, mode TEXT, verdict TEXT,
        at TEXT DEFAULT (datetime('now','localtime')))""")
    c.execute("""CREATE TABLE IF NOT EXISTS recording_summary (
        rec_id TEXT PRIMARY KEY, kind TEXT, summary TEXT, topics TEXT, speakers TEXT, keep TEXT, keep_reason TEXT,
        source TEXT, sample_start REAL, at TEXT DEFAULT (datetime('now','localtime')), user_kind TEXT, user_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS recording_label (
        rec_id TEXT PRIMARY KEY, note TEXT, tags TEXT, at TEXT DEFAULT (datetime('now','localtime')))""")
    c.execute("""CREATE TABLE IF NOT EXISTS recording_moves (
        id INTEGER PRIMARY KEY, rec_id TEXT, from_path TEXT, to_path TEXT, prev_status TEXT,
        at TEXT DEFAULT (datetime('now','localtime')), restored INTEGER DEFAULT 0)""")
    for table, col, typ in MIGRATIONS:
        if col not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    c.commit()


@contextmanager
def tx():
    c = conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


def rows(sql, *args):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql, *args):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def kv_get(key, default=None):
    r = one("SELECT value FROM kv WHERE key=?", key)
    return json.loads(r["value"]) if r else default


def kv_set(key, value):
    with tx() as c:
        c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, json.dumps(value)))


# Cards the Lightning "sweep" filed as log-only are machine decisions (still undoable), not your taste — keep them out of
# anything trained or measured on your decisions. Usage: f"... WHERE status IN (...) AND m.id NOT IN ({SWEPT_SQL})"
SWEPT_SQL = ("SELECT moment_id FROM edits e WHERE kind='sweep' AND via='lightning' AND id=(SELECT max(id) FROM edits "
             "WHERE moment_id=e.moment_id AND kind IN ('sweep','sweep-undo'))")


def ranked():
    """Triage should use the smart order if either the tournament ranking or the keeper/learned score passed its backtest."""
    return bool(kv_get("rank_enabled", False) or kv_get("learned_enabled", False))
