"""
Embed every verse, so topical search has something to search.

    python -m app.ingest.embed_verses --translation BSB

~31,000 verses. On a laptop CPU with the default local model that is about
three to five minutes and roughly 48 MB of vectors. Run it once per translation
you want topical search to work in.

A note on chunking: verses are embedded individually rather than in windows.
Windows would give better thematic recall -- a three-verse window carries more
context than "Jesus wept" alone -- but they blur citation precision, and this
app lives or dies on being able to point at exactly the verse a claim came
from. The cross-reference hub pass in topics.py recovers most of the recall a
window would have bought, without the cost.
"""
import argparse

import numpy as np

from ..db import connect
from ..embed import embed_texts, stamp


def embed_translation(con, translation: str, batch: int = 256, rebuild: bool = False):
    if rebuild:
        con.execute("DELETE FROM verse_vecs WHERE translation = ?", (translation,))
        con.commit()

    todo = con.execute("""
        SELECT v.vid, v.text FROM verses v
        LEFT JOIN verse_vecs e ON e.vid = v.vid AND e.translation = v.translation
        WHERE v.translation = ? AND e.vid IS NULL
        ORDER BY v.vid
    """, (translation,)).fetchall()

    if not todo:
        print(f"  {translation}: already embedded")
        return

    print(f"  {translation}: embedding {len(todo):,} verses ...", flush=True)
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        vecs = embed_texts([r["text"] for r in part])
        con.executemany(
            "INSERT OR REPLACE INTO verse_vecs (vid, translation, embedding) VALUES (?,?,?)",
            [(r["vid"], translation, np.asarray(v, dtype=np.float32).tobytes())
             for r, v in zip(part, vecs)],
        )
        con.commit()
        print(f"    {min(i + batch, len(todo)):,}/{len(todo):,}", end="\r", flush=True)
    stamp(con)
    print(f"\n  {translation}: done")


def rebuild_verse_fts(con, translation: str):
    """Keyword recall alongside vectors — catches proper nouns embeddings blur."""
    con.execute("DELETE FROM verses_fts WHERE translation = ?", (translation,))
    con.execute("""
        INSERT INTO verses_fts (text, translation, vid)
        SELECT text, translation, vid FROM verses WHERE translation = ?
    """, (translation,))
    con.commit()
    print(f"  {translation}: keyword index rebuilt")


def main():
    ap = argparse.ArgumentParser(description="Embed verses for topical search")
    ap.add_argument("--db", default="data/apparatus.db")
    ap.add_argument("--translation", "-t", action="append", default=None,
                    help="repeatable; defaults to every loaded translation")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    con = connect(args.db)
    targets = args.translation or [
        r["translation"] for r in
        con.execute("SELECT DISTINCT translation FROM verses ORDER BY translation")
    ]
    if not targets:
        raise SystemExit("No translations loaded. Run app.ingest.corpus first.")

    for t in targets:
        embed_translation(con, t, rebuild=args.rebuild)
        rebuild_verse_fts(con, t)

    n = con.execute("SELECT COUNT(*) FROM verse_vecs").fetchone()[0]
    print(f"\nTotal verse vectors: {n:,}")
    con.close()


if __name__ == "__main__":
    main()
