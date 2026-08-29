"""
SQLite storage. One file, no server, trivially backed up.

Scale check: ~31k verses x 3 translations, ~340k cross-references, ~14k Strong's
entries, ~150k commentary chunks. That's a few hundred MB and SQLite handles it
without breathing hard. Move to Postgres+pgvector only if you outgrow one box.
"""
import sqlite3
from pathlib import Path
from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS verses (
    vid          INTEGER NOT NULL,
    translation  TEXT NOT NULL,
    text         TEXT NOT NULL,
    PRIMARY KEY (vid, translation)
);
CREATE INDEX IF NOT EXISTS idx_verses_trans ON verses(translation, vid);

-- Public-domain cross-reference set (openbible.info, CC BY).
-- `votes` is their crowd confidence score; we sort by it.
CREATE TABLE IF NOT EXISTS cross_refs (
    from_vid   INTEGER NOT NULL,
    to_start   INTEGER NOT NULL,
    to_end     INTEGER NOT NULL,
    votes      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_xref_from ON cross_refs(from_vid, votes DESC);

-- Strong's Concordance (public domain, via openscriptures).
CREATE TABLE IF NOT EXISTS strongs (
    id          TEXT PRIMARY KEY,          -- 'H430', 'G26'
    lang        TEXT NOT NULL,             -- 'hebrew' | 'greek'
    lemma       TEXT,                      -- אֱלֹהִים
    translit    TEXT,                      -- 'elohiym'
    pronounce   TEXT,
    definition  TEXT,
    derivation  TEXT,
    kjv_usage   TEXT
);

-- Which Strong's numbers appear in which verse. Populated from the
-- Strong's-tagged KJV so we can surface original-language words per verse.
CREATE TABLE IF NOT EXISTS verse_words (
    vid        INTEGER NOT NULL,
    position   INTEGER NOT NULL,
    strongs_id TEXT NOT NULL,
    surface    TEXT,
    PRIMARY KEY (vid, position)
);
CREATE INDEX IF NOT EXISTS idx_vw_vid ON verse_words(vid);
CREATE INDEX IF NOT EXISTS idx_vw_strongs ON verse_words(strongs_id);

-- Every corpus we ingest, with its licence recorded. If a source can't be
-- named and licensed here, it does not go in the database.
CREATE TABLE IF NOT EXISTS sources (
    id        TEXT PRIMARY KEY,   -- 'mhc', 'jfb', 'barnes'
    title     TEXT NOT NULL,
    author    TEXT,
    year      TEXT,
    tradition TEXT,               -- 'reformed', 'wesleyan', 'catholic', ...
    license   TEXT NOT NULL,
    url       TEXT
);

-- Commentary text, chunked and scoped to a verse range.
CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id  TEXT NOT NULL REFERENCES sources(id),
    start_vid  INTEGER NOT NULL,
    end_vid    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    embedding  BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_range ON chunks(start_vid, end_vid);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);

-- Full-text search over commentary, for keyword recall alongside vectors.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='porter'
);

-- Word-by-word original language text (STEPBible TAHOT/TAGNT, CC BY 4.0).
CREATE TABLE IF NOT EXISTS interlinear (
    vid        INTEGER NOT NULL,
    position   INTEGER NOT NULL,
    original   TEXT NOT NULL,      -- ἠγάπησεν  /  בָּרָ֣א
    translit   TEXT,
    gloss      TEXT,               -- contextual English
    gloss_es   TEXT,               -- Spanish, where the source supplies it
    strongs_id TEXT,
    morph      TEXT,               -- decoded: "verb · aorist active indicative"
    morph_code TEXT,               -- raw, e.g. V-AAI-3S
    PRIMARY KEY (vid, position)
);
CREATE INDEX IF NOT EXISTS idx_inter_vid ON interlinear(vid);

-- Verse-level embeddings, for topical search. ~31k rows per translation.
CREATE TABLE IF NOT EXISTS verse_vecs (
    vid         INTEGER NOT NULL,
    translation TEXT NOT NULL,
    embedding   BLOB NOT NULL,
    PRIMARY KEY (vid, translation)
);

-- Keyword search over verse text.
CREATE VIRTUAL TABLE IF NOT EXISTS verses_fts USING fts5(
    text, translation UNINDEXED, vid UNINDEXED, tokenize='porter'
);

-- ── accounts ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name          TEXT,
    is_admin      INTEGER DEFAULT 0,
    created_at    INTEGER,
    last_seen     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at INTEGER,
    expires_at INTEGER,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS login_attempts (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket TEXT NOT NULL,
    ts     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts ON login_attempts(bucket, ts);

-- Who looked at what. Separate from the study cache on purpose: the cache is
-- shared so ten people reading John 3:16 cost one model call, while the fact
-- that YOU read it is yours alone.
CREATE TABLE IF NOT EXISTS user_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    cache_key  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    title      TEXT,
    detail     TEXT,
    lang       TEXT DEFAULT 'en',
    saved      INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, cache_key)
);
CREATE INDEX IF NOT EXISTS idx_uh_user ON user_history(user_id, created_at DESC);

-- The user's own writing. Anchored to a verse range rather than to a study,
-- so a note written while reading Romans 5 surfaces again next time Romans 5
-- comes up — whether that is a passage study, a topic, or sermon prep.
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    start_vid  INTEGER NOT NULL,
    end_vid    INTEGER NOT NULL,
    ref_label  TEXT NOT NULL,
    body       TEXT NOT NULL,
    lang       TEXT DEFAULT 'en',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_notes_range ON notes(start_vid, end_vid);
CREATE INDEX IF NOT EXISTS idx_notes_recent ON notes(updated_at DESC);

-- Which embedding model produced the vectors in this database.
--
-- Vectors from different models are not comparable. Embed the corpus locally
-- with sentence-transformers (384 dims) and then query it in production with
-- Voyage (1024 dims) and the search does not error — it returns confident
-- nonsense, or crashes deep in numpy with a shape error that says nothing
-- about the actual cause. Recording it here makes the mismatch detectable.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- What each model call actually cost, and how often the cache saved one.
--
-- Bible study concentrates hard on a few dozen passages, so the cache hit rate
-- drives spend far more than user count does. Without measuring it you end up
-- designing paywalls around a bill you never had.
CREATE TABLE IF NOT EXISTS usage_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    kind          TEXT NOT NULL,          -- study | topic | sermon | candidates
    cached        INTEGER NOT NULL DEFAULT 0,
    model         TEXT,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(ts DESC);

-- Rate-limit counters. In SQLite rather than memory because gunicorn runs
-- several workers and per-process counters would each enforce the full limit
-- independently — silently multiplying the real ceiling by the worker count.
CREATE TABLE IF NOT EXISTS rate_events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket TEXT NOT NULL,
    kind   TEXT NOT NULL,
    ts     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate ON rate_events(bucket, ts);

-- Reports that something came out wrong. The only way to learn what the tool
-- gets wrong at scale is to make it one tap to say so; without this you find
-- failures by accident, on passages you happened to already know.
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_key  TEXT,
    kind       TEXT,              -- study | topic | sermon
    ref_label  TEXT,
    section    TEXT,              -- which part was wrong
    note       TEXT,
    resolved   INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_feedback_open ON feedback(resolved, created_at DESC);

-- Cache of generated studies. Same passage + same question = same answer,
-- and it makes the app feel instant on re-visits.
CREATE TABLE IF NOT EXISTS studies (
    cache_key   TEXT PRIMARY KEY,
    start_vid   INTEGER NOT NULL,
    end_vid     INTEGER NOT NULL,
    question    TEXT,
    payload     TEXT NOT NULL,        -- validated JSON
    model       TEXT,
    kind        TEXT DEFAULT 'study', -- 'study' | 'sermon'
    title       TEXT,                 -- denormalised so history lists cheaply
    lang        TEXT DEFAULT 'en',
    saved       INTEGER DEFAULT 0,    -- starred; survives history pruning
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_studies_range ON studies(start_vid, end_vid);

-- Cache of topical studies, keyed separately from passage studies.
CREATE TABLE IF NOT EXISTS topics (
    cache_key  TEXT PRIMARY KEY,
    topic      TEXT NOT NULL,
    lang       TEXT NOT NULL DEFAULT 'en',
    payload    TEXT NOT NULL,
    model      TEXT,
    saved      INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# ── which half a table belongs to ────────────────────────────────────
#
# The corpus (scripture, commentary, lexicon, vectors) is rebuilt on a laptop
# and uploaded as a file. Everything a person creates — their account, notes,
# history, and the study cache they paid for — must survive that upload.
#
# Keeping them in one file meant replacing the corpus deleted the people, which
# is exactly what happened. They now live in separate files, attached as one
# database at connection time. SQLite resolves an unqualified table name by
# searching main first and then attached schemas, so every existing query keeps
# working untouched — provided a table exists in only one of them.
USER_TABLES = ['users', 'sessions', 'login_attempts', 'user_history', 'notes', 'feedback', 'usage_log', 'rate_events', 'studies', 'topics']

def _statements(sql: str):
    """
    Split a schema into statements without being fooled by comments.

    Splitting on ";" alone breaks the moment a comment contains one — the
    prose above cross_refs did, and the tail of that sentence became a
    statement of its own. Comments are stripped first.
    """
    out, buf = [], []
    for raw in sql.split("\n"):
        if raw.strip().startswith("--"):
            continue
        # Strip a trailing comment but keep the code before it: a column line
        # like "kind TEXT DEFAULT 'study', -- 'study' | 'sermon'" has no
        # semicolon of its own, while the comment on the NEXT line might, and
        # cutting there lands in the middle of a CREATE TABLE.
        line = raw.split("--")[0].rstrip() if "--" in raw else raw
        buf.append(line)
        if ";" in line:
            stmt = "\n".join(buf).strip()
            if stmt:
                out.append(stmt.rstrip(";").strip())
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        out.append(tail.rstrip(";").strip())
    return [x for x in out if x]


def _names(stmt: str) -> str:
    import re as _re
    m = _re.search(r"(?:TABLE|INDEX)\s+IF NOT EXISTS\s+([A-Za-z_][\w]*)", stmt)
    if not m:
        return ""
    name = m.group(1)
    on = _re.search(r"\bON\s+([A-Za-z_][\w]*)", stmt)
    return on.group(1) if on else name


_ALL = _statements(SCHEMA)
USER_SCHEMA = ";\n".join(
    st.replace("IF NOT EXISTS ", "IF NOT EXISTS userdata.", 1)
    for st in _ALL if _names(st) in USER_TABLES
) + ";"
CORPUS_SCHEMA = ";\n".join(st for st in _ALL if _names(st) not in USER_TABLES) + ";"


def user_db_path(main_path: str) -> str:
    """Sits beside the corpus file unless told otherwise."""
    import os as _os
    explicit = _os.environ.get("USER_DB_PATH", "").strip()
    if explicit:
        return explicit
    d = _os.path.dirname(main_path) or "."
    return _os.path.join(d, "userdata.db")


def split_existing(con, user_path: str):
    """
    One-time move of user tables out of a combined database.

    Copies first, verifies the row counts match, and only then drops the
    originals — because getting this wrong deletes the accounts it exists to
    protect. If anything does not line up, it leaves both copies alone and
    says so; a duplicated table is recoverable, a dropped one is not.
    """
    import sys
    # Gunicorn boots several workers at once and every one of them calls this.
    # Two processes copying and dropping the same tables can duplicate rows or
    # fail halfway. BEGIN IMMEDIATE takes the write lock up front, so the first
    # worker does the work and the rest find nothing left to do.
    try:
        con.execute("BEGIN IMMEDIATE")
    except Exception:
        return
    moved = []
    for t in USER_TABLES:
        try:
            in_main = con.execute(
                "SELECT COUNT(*) FROM main.sqlite_master WHERE type='table' AND name=?",
                (t,)).fetchone()[0]
        except Exception:
            continue
        if not in_main:
            continue
        n_main = con.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
        n_user = con.execute(f"SELECT COUNT(*) FROM userdata.{t}").fetchone()[0]
        if n_user:
            # Already split and something re-created the old copy. Leave it.
            continue
        if n_main:
            # Copy by explicit column name, not SELECT *. The old table has
            # columns that later migrations added; the freshly created one has
            # only what the base schema declares, and the orders will not match
            # either. Intersecting the two is the only safe copy.
            src = [r[1] for r in con.execute(f"PRAGMA main.table_info({t})")]
            dst = [r[1] for r in con.execute(f"PRAGMA userdata.table_info({t})")]
            shared = [c for c in src if c in dst]
            if not shared:
                print(f"[split] {t}: no shared columns; leaving it alone", file=sys.stderr)
                continue
            cols = ", ".join(shared)
            con.execute(f"INSERT INTO userdata.{t} ({cols}) SELECT {cols} FROM main.{t}")
            check = con.execute(f"SELECT COUNT(*) FROM userdata.{t}").fetchone()[0]
            if check != n_main:
                print(f"[split] {t}: copied {check} of {n_main}; leaving both in place",
                      file=sys.stderr)
                con.rollback()
                return
        con.execute(f"DROP TABLE main.{t}")
        moved.append(f"{t}({n_main})")
    con.commit()
    if moved:
        print(f"[split] moved to {user_path}: {', '.join(moved)}", flush=True)


# Columns added after the first release. SQLite has no ADD COLUMN IF NOT
# EXISTS, so check the table first — this runs on every boot and must be a
# no-op once applied.
_ADDED_COLUMNS = {
    "studies": [("kind", "TEXT DEFAULT 'study'"), ("title", "TEXT"),
                ("lang", "TEXT DEFAULT 'en'"), ("saved", "INTEGER DEFAULT 0")],
    "topics":  [("saved", "INTEGER DEFAULT 0")],
    "notes":    [("lang", "TEXT DEFAULT 'en'"), ("user_id", "INTEGER")],
    "feedback": [("user_id", "INTEGER")],
    "usage_log":[("user_id", "INTEGER"), ("cache_read", "INTEGER DEFAULT 0"),
                 ("cache_write", "INTEGER DEFAULT 0")],
}


def migrate(con, schema: str = ""):
    """Add columns introduced after the first release. Idempotent."""
    prefix = f"{schema}." if schema else ""
    for table, cols in _ADDED_COLUMNS.items():
        try:
            have = {r[1] for r in con.execute(f"PRAGMA {prefix}table_info({table})")}
        except Exception:
            continue
        if not have:
            continue
        for name, decl in cols:
            if name not in have:
                con.execute(f"ALTER TABLE {prefix}{table} ADD COLUMN {name} {decl}")
    con.commit()


def get_db():
    """Per-request connection, from whichever backend store.py resolved."""
    if "db" not in g:
        from . import store
        g.db = store.connect()
    return g.db


def close_db(_=None):
    db = g.pop("db", None)
    if db is not None:
        from . import store
        store.release(db)


def connect(path: str) -> sqlite3.Connection:
    """
    Open the corpus and attach the user database as one logical database.

    Both halves are created if missing, and a combined database left over from
    before the split is migrated on first open.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    upath = user_db_path(path)
    Path(upath).parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(path, check_same_thread=False, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("ATTACH DATABASE ? AS userdata", (upath,))
    con.executescript(CORPUS_SCHEMA)
    con.executescript(USER_SCHEMA)
    # Bring the user file up to date before moving anything into it, or the
    # copy lands in tables that are missing the newer columns.
    migrate(con, schema="userdata")
    split_existing(con, upath)
    migrate(con, schema="userdata")
    # Ingest writes a lot; these make it bearable.
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    migrate(con)
    return con


def init_db(path: str):
    con = connect(path)
    migrate(con)
    con.close()
