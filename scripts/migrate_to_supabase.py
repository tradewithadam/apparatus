"""
Push a locally-ingested SQLite database into Supabase Postgres.

    python scripts/migrate_to_supabase.py \
        --sqlite data/apparatus.db \
        --pg "postgresql://postgres.abc:PW@aws-0-us-east-1.pooler.supabase.com:5432/postgres"

Run this from your laptop, not from Render. It is idempotent -- safe to re-run
after adding a commentary.

CONNECTION STRING, THE PART THAT WASTES AN AFTERNOON
Supabase gives you several. Use the SESSION POOLER one (port 5432,
host contains `pooler.supabase.com`).

  - The *direct* connection is IPv6-only unless you pay for the IPv4 add-on,
    and Render's outbound traffic is IPv4. It will simply hang.
  - The *transaction* pooler (port 6543) does not support prepared statements,
    which psycopg uses by default. It will fail in confusing ways.

Session pooler, port 5432. Dashboard -> Connect -> Session pooler.
"""
import argparse
import os
import sqlite3
import sys

import psycopg
from psycopg import sql as _sql

DDL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS verses (
    vid INTEGER NOT NULL, translation TEXT NOT NULL, text TEXT NOT NULL,
    PRIMARY KEY (vid, translation)
);
CREATE INDEX IF NOT EXISTS idx_verses_trans ON verses(translation, vid);

CREATE TABLE IF NOT EXISTS cross_refs (
    from_vid INTEGER NOT NULL, to_start INTEGER NOT NULL,
    to_end INTEGER NOT NULL, votes INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_xref_from ON cross_refs(from_vid, votes DESC);

CREATE TABLE IF NOT EXISTS strongs (
    id TEXT PRIMARY KEY, lang TEXT NOT NULL, lemma TEXT, translit TEXT,
    pronounce TEXT, definition TEXT, derivation TEXT, kjv_usage TEXT
);

CREATE TABLE IF NOT EXISTS verse_words (
    vid INTEGER NOT NULL, position INTEGER NOT NULL,
    strongs_id TEXT NOT NULL, surface TEXT,
    PRIMARY KEY (vid, position)
);
CREATE INDEX IF NOT EXISTS idx_vw_vid ON verse_words(vid);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT, year TEXT,
    tradition TEXT, license TEXT NOT NULL, url TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
    id BIGINT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    start_vid INTEGER NOT NULL,
    end_vid INTEGER NOT NULL,
    text TEXT NOT NULL,
    embedding halfvec({dim}),
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);
CREATE INDEX IF NOT EXISTS idx_chunks_range ON chunks(start_vid, end_vid);
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING GIN(tsv);

CREATE TABLE IF NOT EXISTS studies (
    cache_key TEXT PRIMARY KEY,
    start_vid INTEGER NOT NULL, end_vid INTEGER NOT NULL,
    question TEXT, payload TEXT NOT NULL, model TEXT,
    kind TEXT DEFAULT 'study', title TEXT, lang TEXT DEFAULT 'en',
    saved INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_studies_recent ON studies(created_at DESC);

CREATE TABLE IF NOT EXISTS topics (
    cache_key TEXT PRIMARY KEY, topic TEXT NOT NULL,
    lang TEXT NOT NULL DEFAULT 'en', payload TEXT NOT NULL, model TEXT,
    saved INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_topics_recent ON topics(created_at DESC);
"""

# Built after the rows land -- indexing an empty table produces a useless index.
VECTOR_INDEX = """
CREATE INDEX IF NOT EXISTS idx_chunks_vec ON chunks
USING hnsw (embedding halfvec_cosine_ops);
"""


def copy_table(lite, pg, table, columns, pk=None, transform=None, batch=5000):
    cols = ", ".join(columns)
    total = lite.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if not total:
        print(f"  {table:<12} empty, skipped")
        return

    conflict = (f"ON CONFLICT ({pk}) DO NOTHING" if pk else "")
    placeholders = ", ".join(["%s"] * len(columns))
    stmt = f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) {conflict}"

    with pg.cursor() as cur:
        cur.execute(f"TRUNCATE {table} CASCADE" if not pk else f"DELETE FROM {table}")
        done = 0
        batch_rows = []
        for row in lite.execute(f"SELECT {cols} FROM {table}"):
            vals = transform(row) if transform else tuple(row)
            if vals is None:
                continue
            batch_rows.append(vals)
            if len(batch_rows) >= batch:
                cur.executemany(stmt, batch_rows)
                done += len(batch_rows)
                batch_rows.clear()
                print(f"  {table:<12} {done:>8,}/{total:,}", end="\r", flush=True)
        if batch_rows:
            cur.executemany(stmt, batch_rows)
            done += len(batch_rows)
    pg.commit()
    print(f"  {table:<12} {done:>8,} rows          ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", default="data/apparatus.db")
    ap.add_argument("--pg", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--skip-chunks", action="store_true",
                    help="scripture and lexicon only; useful for a first deploy")
    args = ap.parse_args()

    if not args.pg:
        sys.exit("Need --pg or DATABASE_URL (use the Supabase SESSION pooler, port 5432)")
    if not os.path.exists(args.sqlite):
        sys.exit(f"No such file: {args.sqlite}. Run the ingest first.")

    lite = sqlite3.connect(args.sqlite)

    import numpy as np
    sample = lite.execute(
        "SELECT embedding FROM chunks WHERE embedding IS NOT NULL LIMIT 1").fetchone()
    dim = len(np.frombuffer(sample[0], dtype=np.float32)) if sample else 512
    print(f"Embedding dimension: {dim}" + ("" if sample else "  (no vectors yet; using default)"))

    pg = psycopg.connect(args.pg)
    print("Creating schema ...")
    with pg.cursor() as cur:
        cur.execute(DDL.format(dim=dim))
    pg.commit()

    print("Copying ...")
    copy_table(lite, pg, "sources",
               ["id", "title", "author", "year", "tradition", "license", "url"], pk="id")
    copy_table(lite, pg, "verses", ["vid", "translation", "text"], pk="vid, translation")
    copy_table(lite, pg, "cross_refs", ["from_vid", "to_start", "to_end", "votes"])
    copy_table(lite, pg, "strongs",
               ["id", "lang", "lemma", "translit", "pronounce",
                "definition", "derivation", "kjv_usage"], pk="id")
    copy_table(lite, pg, "verse_words",
               ["vid", "position", "strongs_id", "surface"], pk="vid, position")

    if not args.skip_chunks:
        def vec(row):
            cid, sid, sv, ev, txt, emb = row
            if emb is None:
                return (cid, sid, sv, ev, txt, None)
            arr = np.frombuffer(emb, dtype=np.float32)
            return (cid, sid, sv, ev, txt,
                    "[" + ",".join(f"{float(x):.5f}" for x in arr) + "]")

        copy_table(lite, pg, "chunks",
                   ["id", "source_id", "start_vid", "end_vid", "text", "embedding"],
                   pk="id", transform=vec, batch=500)

        print("Building vector index (this takes a minute) ...")
        with pg.cursor() as cur:
            cur.execute(VECTOR_INDEX)
        pg.commit()

    with pg.cursor() as cur:
        cur.execute("ANALYZE")
        cur.execute("""
            SELECT pg_size_pretty(pg_database_size(current_database())) AS size
        """)
        size = cur.fetchone()[0]
    pg.commit()

    print(f"\nDone. Database size: {size}")
    print("Supabase free tier is 500 MB — if you are close, cut a commentary or upgrade.")
    lite.close()
    pg.close()


if __name__ == "__main__":
    main()
