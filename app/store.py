"""
Storage backend: SQLite locally, Postgres (Supabase) in production.

The split is deliberate:

  ingest  -> always SQLite, always on your machine. Ingestion downloads
             hundreds of MB and runs for ten minutes; you do not want that
             happening on a 512 MB Render instance, and you do not want to
             re-run it on every deploy.

  serve   -> whichever DATABASE_URL points at. Local dev reads the SQLite file
             directly. Production reads Supabase.

  bridge  -> scripts/migrate_to_supabase.py, run once from your laptop.

So the ingest code never learns Postgres exists, and the serving code speaks
both. Write SQL in SQLite's `?` placeholder style; this module rewrites it for
psycopg on the way through.
"""
import os
import re

DATABASE_URL = os.environ.get("DATABASE_URL", "")
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

_pool = None


# ---------------------------------------------------------------- connections

def connect():
    """Open a connection to whichever backend is configured."""
    if IS_PG:
        return _pg_pool().getconn()
    import sqlite3
    # check_same_thread=False because gunicorn now runs threaded workers, and a
    # streaming response is created in one thread and iterated in another. Each
    # request and each stream opens its own connection and closes it — nothing
    # is shared concurrently — so the check is protecting against a situation
    # that cannot arise here, while breaking one that does.
    con = sqlite3.connect(os.environ.get("DB_PATH", "data/apparatus.db"),
                          check_same_thread=False, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 20000")
    return con


def release(con):
    if IS_PG:
        _pg_pool().putconn(con)
    else:
        con.close()


def _pg_pool():
    """
    A small connection pool. Supabase's free tier caps connections hard, and
    Render can run several gunicorn workers, so keep max_size modest --
    workers x max_size is the number that has to stay under the cap.
    """
    global _pool
    if _pool is None:
        from psycopg_pool import ConnectionPool
        from psycopg.rows import dict_row
        _pool = ConnectionPool(
            DATABASE_URL,
            min_size=1,
            max_size=int(os.environ.get("PG_POOL_SIZE", "3")),
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


# ---------------------------------------------------------------- query layer

_PLACEHOLDER = re.compile(r"\?")


def _adapt(sql: str) -> str:
    return _PLACEHOLDER.sub("%s", sql) if IS_PG else sql


def rows(con, sql: str, params=()) -> list[dict]:
    """Run a SELECT written in `?` style, get back a list of plain dicts."""
    if IS_PG:
        with con.cursor() as cur:
            cur.execute(_adapt(sql), params)
            return [dict(r) for r in cur.fetchall()]
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def one(con, sql: str, params=()):
    """First column of the first row, or None."""
    r = rows(con, sql, params)
    if not r:
        return None
    return next(iter(r[0].values()))


def execute(con, sql: str, params=()):
    if IS_PG:
        with con.cursor() as cur:
            cur.execute(_adapt(sql), params)
        con.commit()
    else:
        con.execute(sql, params)
        con.commit()


def upsert_study(con, key, start, end, question, payload, model,
                 kind="study", title=None, lang="en"):
    """
    Same intent, different dialect.

    Note the SQLite branch preserves `saved` rather than using INSERT OR
    REPLACE — replacing the row would silently unstar anything the user had
    kept, the moment they re-ran the same study.
    """
    args = (key, start, end, question, payload, model, kind, title, lang)
    if IS_PG:
        execute(con, """
            INSERT INTO studies (cache_key, start_vid, end_vid, question,
                                 payload, model, kind, title, lang)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT (cache_key) DO UPDATE
              SET payload = EXCLUDED.payload, model = EXCLUDED.model,
                  title = EXCLUDED.title
        """, args)
    else:
        execute(con, """
            INSERT INTO studies (cache_key, start_vid, end_vid, question,
                                 payload, model, kind, title, lang)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(cache_key) DO UPDATE SET
              payload = excluded.payload, model = excluded.model,
              title = excluded.title
        """, args)


def upsert_topic(con, key, topic, lang, payload, model):
    args = (key, topic, lang, payload, model)
    sql = ("""INSERT INTO topics (cache_key, topic, lang, payload, model)
              VALUES (?,?,?,?,?)
              ON CONFLICT (cache_key) DO UPDATE SET payload = EXCLUDED.payload"""
           if IS_PG else
           """INSERT INTO topics (cache_key, topic, lang, payload, model)
              VALUES (?,?,?,?,?)
              ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload""")
    execute(con, sql, args)


def history(con, limit: int = 60, saved_only: bool = False,
            lang: str | None = None, q: str | None = None):
    """
    Recent studies, sermons, and topics, newest first.

    Reads the denormalised title/kind columns rather than parsing every stored
    payload — a history list should not deserialise sixty JSON blobs to render.
    """
    args_s, args_t = [], []
    cond_s, cond_t = "", ""
    if saved_only:
        cond_s += " AND COALESCE(saved,0) = 1"
        cond_t += " AND COALESCE(saved,0) = 1"
    if lang:
        cond_s += " AND COALESCE(lang,'en') = ?"; args_s.append(lang)
        cond_t += " AND COALESCE(lang,'en') = ?"; args_t.append(lang)
    if q:
        # Matches the reference, the question asked, and the topic text, so
        # "justified" finds the Romans study you ran by question rather than
        # by reference.
        like = f"%{q.lower()}%"
        cond_s += (" AND (LOWER(COALESCE(title,'')) LIKE ?"
                   " OR LOWER(COALESCE(question,'')) LIKE ?)")
        args_s += [like, like]
        cond_t += " AND LOWER(topic) LIKE ?"
        args_t.append(like)

    rows = rows_or_empty(con, f"""
        SELECT cache_key, COALESCE(kind,'study') AS kind,
               COALESCE(title, question, '') AS title,
               question AS detail, COALESCE(lang,'en') AS lang,
               COALESCE(saved,0) AS saved, created_at
        FROM studies WHERE 1=1 {cond_s}
        ORDER BY created_at DESC LIMIT ?
    """, (*args_s, limit))

    rows += rows_or_empty(con, f"""
        SELECT cache_key, 'topic' AS kind, topic AS title,
               NULL AS detail, COALESCE(lang,'en') AS lang,
               COALESCE(saved,0) AS saved, created_at
        FROM topics WHERE 1=1 {cond_t}
        ORDER BY created_at DESC LIMIT ?
    """, (*args_t, limit))

    rows.sort(key=lambda r: str(r["created_at"]), reverse=True)
    return rows[:limit]


def rows_or_empty(con, sql, params=()):
    """History must not 500 because one table is missing on an older database."""
    try:
        return rows(con, sql, params)
    except Exception:
        return []


def load_entry(con, key: str):
    for table in ("studies", "topics"):
        r = rows_or_empty(con, f"SELECT payload FROM {table} WHERE cache_key = ?", (key,))
        if r:
            return r[0]["payload"]
    return None


def set_saved(con, key: str, saved: bool) -> bool:
    hit = False
    for table in ("studies", "topics"):
        try:
            execute(con, f"UPDATE {table} SET saved = ? WHERE cache_key = ?",
                    (1 if saved else 0, key))
            if rows_or_empty(con, f"SELECT 1 AS x FROM {table} WHERE cache_key = ?", (key,)):
                hit = True
        except Exception:
            pass
    return hit


def delete_entry(con, key: str):
    for table in ("studies", "topics"):
        try:
            execute(con, f"DELETE FROM {table} WHERE cache_key = ?", (key,))
        except Exception:
            pass


# ---------------------------------------------------------------- vectors

_USABLE = None


def has_vectors(con) -> bool:
    """
    Are there vectors AND can this backend actually query them?

    The second half matters. Checking only for the presence of vectors meant
    every search embedded the query first and discovered the dimension
    mismatch afterwards — burning an API call, and on a rate-limited key
    turning a degraded feature into a 500 for the whole study. Decide before
    spending the call, not after.
    """
    global _USABLE
    if rows(con, "SELECT 1 AS x FROM chunks WHERE embedding IS NOT NULL LIMIT 1") == []:
        return False
    if _USABLE is None:
        try:
            from . import embed
            _USABLE = embed.check(con) is None
            if not _USABLE:
                import sys
                print("[embed] stored vectors do not match EMBED_BACKEND; "
                      "semantic search disabled (everything else works)",
                      file=sys.stderr)
        except Exception:
            _USABLE = True
    return _USABLE


_VEC_WARNED = False


def vector_search(con, qvec, k: int, exclude_ids: set[int]) -> list[tuple[int, float]]:
    """
    Nearest chunks by cosine similarity. Returns (chunk_id, score) with score in
    [0,1], higher is closer, in both backends.

    Postgres does this in the database with a pgvector index -- the right answer
    once the corpus is real. SQLite falls back to a numpy scan, which is fine at
    laptop scale and keeps local dev dependency-free.
    """
    if IS_PG:
        lit = "[" + ",".join(f"{float(x):.6f}" for x in qvec) + "]"
        # `1 - (a <=> b)` converts pgvector's cosine *distance* to similarity.
        sql = """
            SELECT id, 1 - (embedding <=> %s::vector) AS score
            FROM chunks
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """
        with con.cursor() as cur:
            cur.execute(sql, (lit, lit, k + len(exclude_ids)))
            out = [(r["id"], float(r["score"])) for r in cur.fetchall()]
        return [x for x in out if x[0] not in exclude_ids][:k]

    import numpy as np
    ids, mat = [], []
    for row in con.execute("SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL"):
        if row["id"] in exclude_ids:
            continue
        ids.append(row["id"])
        mat.append(np.frombuffer(row["embedding"], dtype=np.float32))
    if not ids:
        return []
    mat = np.vstack(mat)
    # Dimension mismatch means the query model differs from the one that built
    # the index. Returning [] degrades to range and keyword retrieval, which is
    # honest; multiplying mismatched vectors would either crash or, worse, rank
    # results by noise and present them as relevant.
    if mat.shape[1] != len(qvec):
        global _VEC_WARNED
        if not _VEC_WARNED:
            import sys
            print(f"[embed] stored vectors are {mat.shape[1]}-dim but queries are "
                  f"{len(qvec)}-dim — semantic search disabled. Re-embed with the "
                  f"current EMBED_BACKEND.", file=sys.stderr)
            _VEC_WARNED = True
        return []
    mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    q = np.asarray(qvec, dtype=np.float32)
    q = q / (np.linalg.norm(q) + 1e-9)
    scores = mat @ q
    top = np.argsort(-scores)[:k]
    return [(ids[i], float(scores[i])) for i in top]


# ---------------------------------------------------------------- keyword

def keyword_search(con, query: str, limit: int = 4) -> list[dict]:
    """
    Full-text recall. SQLite uses FTS5, Postgres uses tsvector; the results are
    close enough in practice and both beat embeddings on rare proper nouns.
    """
    safe = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in query)
    terms = [t for t in safe.split() if len(t) > 2][:8]
    if not terms:
        return []

    if IS_PG:
        return rows(con, """
            SELECT c.id, c.source_id, c.start_vid, c.end_vid, c.text,
                   s.title, s.author, s.year, s.tradition, s.license
            FROM chunks c
            JOIN sources s ON s.id = c.source_id
            WHERE c.tsv @@ websearch_to_tsquery('english', ?)
            ORDER BY ts_rank(c.tsv, websearch_to_tsquery('english', ?)) DESC
            LIMIT ?
        """, (" OR ".join(terms), " OR ".join(terms), limit))

    import sqlite3
    try:
        return rows(con, """
            SELECT c.id, c.source_id, c.start_vid, c.end_vid, c.text,
                   s.title, s.author, s.year, s.tradition, s.license
            FROM chunks_fts f
            JOIN chunks c ON c.id = f.rowid
            JOIN sources s ON s.id = c.source_id
            WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?
        """, (" OR ".join(terms), limit))
    except sqlite3.OperationalError:
        return []
