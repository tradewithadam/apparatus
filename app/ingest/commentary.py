"""
Commentary ingestion + embedding.

    python -m app.ingest.commentary --dir corpora/ --embed

WHY MULTIPLE COMMENTATORS, AND WHICH ONES

The `disputed` feature only works if the evidence packet actually contains
disagreement. Ingest one commentator and the model has nothing to disagree with,
so it will either invent the other side or quietly present one tradition as
the Christian position. Both are the failure you built this to avoid.

So ingest breadth on purpose, and record each one's tradition:

  Matthew Henry (1710)      devotional, warm            puritan
  Jamieson-Fausset-Brown    concise, critical           reformed
  Albert Barnes (1830s)     detailed, exegetical        presbyterian
  John Gill (1748)          heavy, verbose              particular baptist
  Adam Clarke (1831)        philological, independent   methodist / wesleyan
  John Wesley's Notes       brief, practical            methodist
  Keil & Delitzsch (OT)     technical Hebrew            lutheran
  Catena Aurea (Aquinas)    patristic chains            catholic / patristic

All public domain, all on CCEL (ccel.org) and StudyLight in bulk formats.
Clarke and Wesley pull the centre of gravity away from an all-Reformed panel;
Catena Aurea brings the church fathers in. That spread is what makes an
even-handed `disputed` section possible.

EXPECTED INPUT FORMAT

One JSONL file per commentary, named <source_id>.jsonl, with a leading metadata
line, then one object per comment block:

  {"_source": {"id":"mhc","title":"Matthew Henry's Commentary",
               "author":"Matthew Henry","year":"1710","tradition":"puritan",
               "license":"public domain","url":"https://ccel.org/ccel/henry/mhc"}}
  {"ref": "Rom 5:1-5", "text": "..." }
  {"ref": "Rom 5:6",   "text": "..." }

Getting public-domain commentary into that shape is a scraping/parsing job that
varies per source; keep those converters in scripts/ and out of the app.
"""
import argparse
import glob
import json
import os
import re
import sys

import sqlite3

import numpy as np

from .. import refs
from ..db import connect
from ..embed import embed_texts, stamp, signature

MAX_CHARS = 1400      # ~350 tokens: big enough for an argument, small enough to rank
OVERLAP = 180


def chunk_text(text: str) -> list[str]:
    """Split on paragraph boundaries, then sentences, never mid-word."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= MAX_CHARS:
        return [text] if text else []

    sentences = re.split(r"(?<=[.!?;])\s+", text)
    chunks, cur = [], ""
    for s in sentences:
        if len(cur) + len(s) + 1 <= MAX_CHARS:
            cur = f"{cur} {s}".strip()
        else:
            if cur:
                chunks.append(cur)
            cur = (cur[-OVERLAP:] + " " + s).strip() if cur else s
            while len(cur) > MAX_CHARS:
                chunks.append(cur[:MAX_CHARS])
                cur = cur[MAX_CHARS - OVERLAP:]
    if cur:
        chunks.append(cur)
    return chunks


def load_file(con, path: str) -> int:
    src = None
    pending = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                print(f"  ! {os.path.basename(path)}:{lineno} bad JSON, skipped", file=sys.stderr)
                continue

            if "_source" in obj:
                src = obj["_source"]
                if not src.get("license"):
                    raise SystemExit(
                        f"{path}: _source has no license field. Every corpus must "
                        f"declare its licence before it enters the database."
                    )
                con.execute("""
                    INSERT OR REPLACE INTO sources
                      (id, title, author, year, tradition, license, url)
                    VALUES (?,?,?,?,?,?,?)
                """, (src["id"], src["title"], src.get("author"), src.get("year"),
                      src.get("tradition"), src["license"], src.get("url")))
                continue

            if src is None:
                raise SystemExit(f"{path}: first line must be a _source record")

            try:
                start, end = refs.parse(obj["ref"])
            except (refs.RefError, KeyError):
                continue

            for c in chunk_text(obj.get("text", "")):
                pending.append((src["id"], start, end, c))

    if pending:
        con.execute("DELETE FROM chunks WHERE source_id = ?", (pending[0][0],))
        con.executemany(
            "INSERT INTO chunks (source_id, start_vid, end_vid, text) VALUES (?,?,?,?)",
            pending,
        )
        con.commit()
    return len(pending)


def rebuild_fts(con):
    """
    Rebuild the full-text index.

    chunks_fts is an EXTERNAL CONTENT table (content='chunks'), which means it
    stores no text of its own — it indexes rows in `chunks`. Ordinary DML
    against it is not supported: `DELETE FROM chunks_fts` makes FTS5 look for
    matching content rows to un-index, and when they no longer line up it
    leaves the index inconsistent. SQLite then reports "database disk image is
    malformed" on the next touch, which sounds like disk failure and is not.

    The supported form is the special 'rebuild' command below.
    """
    try:
        con.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        con.commit()
        print("  full-text index rebuilt")
    except sqlite3.DatabaseError as e:
        # Keyword search is a supplement to vector search, not a dependency.
        # Losing it should not cost the user a 20-minute ingest.
        print(f"  ! full-text index failed ({e}); keyword search disabled",
              file=sys.stderr)


def embed_all(con, batch: int = 256, force: bool = False):
    """
    Embed chunks that need it.

    "Need it" includes chunks embedded by a DIFFERENT model. Only checking for
    NULL means that switching EMBED_BACKEND and re-running looks like it worked
    — it prints "already current" and changes nothing — leaving half a database
    on one vector space and half on another. That failure reaches production
    looking healthy, which is the worst kind.
    """
    row = con.execute("SELECT value FROM meta WHERE key = 'embedding'").fetchone()
    stored = row[0] if row else None
    changed = stored is not None and stored != signature()

    if changed or force:
        why = "backend changed" if changed else "--reembed"
        print(f"  re-embedding everything ({why}: {stored} -> {signature()})")
        con.execute("UPDATE chunks SET embedding = NULL")
        con.commit()

    todo = [(r["id"], r["text"]) for r in
            con.execute("SELECT id, text FROM chunks WHERE embedding IS NULL")]
    if not todo:
        print(f"  embeddings: already current ({stored or signature()})")
        return
    print(f"  embedding {len(todo):,} chunks ...", flush=True)
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        vecs = embed_texts([t for _, t in part])
        con.executemany(
            "UPDATE chunks SET embedding = ? WHERE id = ?",
            [(np.asarray(v, dtype=np.float32).tobytes(), cid)
             for (cid, _), v in zip(part, vecs)],
        )
        con.commit()
        print(f"    {min(i + batch, len(todo)):,}/{len(todo):,}", end="\r", flush=True)
    stamp(con)
    print("\n  embeddings: done")


def main():
    ap = argparse.ArgumentParser(description="Ingest public-domain commentary")
    ap.add_argument("--db", default="data/apparatus.db")
    ap.add_argument("--dir", default="corpora", help="directory of *.jsonl files")
    ap.add_argument("--embed", action="store_true")
    ap.add_argument("--reembed", action="store_true",
                    help="force re-embedding even if the backend is unchanged")
    args = ap.parse_args()

    con = connect(args.db)
    files = sorted(glob.glob(os.path.join(args.dir, "*.jsonl")))
    if not files:
        print(f"No .jsonl files in {args.dir}/ — see this module's docstring "
              f"for the expected format.")
    for fp in files:
        n = load_file(con, fp)
        print(f"  {os.path.basename(fp)}: {n:,} chunks")

    if files:
        rebuild_fts(con)
    if args.embed or args.reembed:
        embed_all(con, force=args.reembed)

    counts = con.execute("""
        SELECT s.tradition, COUNT(*) n FROM chunks c
        JOIN sources s ON s.id=c.source_id GROUP BY s.tradition ORDER BY n DESC
    """).fetchall()
    if counts:
        print("\nCorpus balance by tradition:")
        for r in counts:
            print(f"  {r['tradition'] or 'unspecified':<24} {r['n']:>7,}")
        if len(counts) < 3:
            print("\n  Note: fewer than three traditions represented. The disputed-"
                  "questions feature needs breadth to work honestly.")
    con.close()


if __name__ == "__main__":
    main()
