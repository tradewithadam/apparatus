"""
Package the local database for upload.

    python scripts/prepare_deploy.py

Strips personal data, VACUUMs, gzips, and reports the size. Upload the result
as a GitHub Release asset, then set DB_URL on Render to its download link. The
app fetches it once onto the persistent disk.

WHY IT STRIPS BY DEFAULT

Your working database holds your notes, your search history, and every study
you have run. A GitHub Release asset on a public repo is a public URL — no
login, no referrer check. Shipping the file as-is publishes your private study
notes to anyone who finds the link, and you would have no way to know.

What ships is the corpus: scripture, lexicon, cross-references, commentary,
embeddings. What stays behind is everything you wrote or ran. Pass --keep-mine
if you deliberately want your own history on the server.
"""
import argparse
import gzip
import os
import shutil
import sqlite3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/apparatus.db")
    ap.add_argument("--out", default="apparatus-db.sqlite.gz")
    ap.add_argument("--keep-mine", action="store_true",
                    help="ship your notes and history too (default: strip them)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(f"No database at {args.db}")

    before = os.path.getsize(args.db) / 1e6
    print(f"source: {before:.0f} MB")

    tmp = args.db + ".vacuum"
    if os.path.exists(tmp):
        os.remove(tmp)

    # Copy first, strip the copy. Never mutate the working database — a bug
    # here would silently delete the user's own notes.
    staging = args.db + ".staging"
    if os.path.exists(staging):
        os.remove(staging)
    shutil.copy2(args.db, staging)

    # Everything a person owns is DROPPED from the upload, not just emptied.
    #
    # Emptying was not enough. On the server these tables now live in a separate
    # attached file, and SQLite resolves an unqualified name in `main` first —
    # so shipping an empty `users` table inside the corpus would shadow the real
    # one and every account would appear to vanish on deploy. Dropping them
    # entirely leaves the attached copies as the only ones.
    PERSONAL = ["users", "sessions", "login_attempts", "user_history", "notes",
                "feedback", "usage_log", "rate_events", "studies", "topics"]
    con = sqlite3.connect(staging)
    if args.keep_mine:
        print("keeping your notes and history (--keep-mine)")
    else:
        removed = []
        for t in PERSONAL:
            try:
                n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                con.execute(f"DROP TABLE IF EXISTS {t}")
                removed.append(f"{t}({n:,})" if n else t)
            except sqlite3.Error:
                pass
        con.commit()
        print("removed from the upload:", ", ".join(removed) if removed else "none found")
    con.close()

    print("vacuuming ...", flush=True)
    con = sqlite3.connect(staging)
    con.execute("VACUUM INTO ?", (tmp,))
    con.close()
    os.remove(staging)
    vac = os.path.getsize(tmp) / 1e6
    print(f"vacuumed: {vac:.0f} MB")

    print("compressing ...", flush=True)
    with open(tmp, "rb") as src, gzip.open(args.out, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
    os.remove(tmp)

    final = os.path.getsize(args.out) / 1e6
    print(f"\n{args.out}  {final:.0f} MB  ({final/before*100:.0f}% of original)")
    if final > 2000:
        print("Over 2 GB — GitHub Release assets cap there. Drop a commentary "
              "or host the file elsewhere.")
    if not args.keep_mine:
        print("\nContains: scripture, lexicon, cross-references, commentary, embeddings.")
        print("Does NOT contain: accounts, notes, history, or the study cache —")
        print("those live in userdata.db on the server and survive this upload.")
    print("\nNext:")
    print("  1. Upload as a GitHub Release asset")
    print("  2. On Render, set DB_URL to its download URL")
    print("  3. Deploy; it downloads once onto the persistent disk")


if __name__ == "__main__":
    main()
