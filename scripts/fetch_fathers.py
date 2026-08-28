"""
Convert the Nicene and Post-Nicene Fathers homilies into commentary JSONL.

    python scripts/fetch_fathers.py --out corpora
    python -m app.ingest.commentary --dir corpora --embed

WHY THIS EXISTS

The SWORD corpus is overwhelmingly 1550-1930 — Reformation and after. If your
reason for trusting a reading is that the church has held it a long time, then
a library whose oldest verse-by-verse voice is Matthew Henry (1710) does not
actually support that principle. These do: Chrysostom preaching through Romans
in Antioch around 390, Augustine on John around 410.

All public domain (Schaff's 19th-century translations, long expired), and CCEL
publishes them as plain text.

THE FORMAT

CCEL's plain text is prose, not a verse-indexed database, but the homilies are
regular:

       Homily II.

       Rom. I. 8, 9

       "First, I thank my God through Jesus Christ..."

       [exposition until the next Homily heading]

So: the heading, then the passage in Roman-numeral chapter form, then the body.
Each homily becomes one entry covering the verses it names. Chrysostom expounds
a block at a time, so the ranges are wide — which is honest, since that is how
he preached.

A HONEST LIMITATION

The reference line gives the verses the homily *starts* from, not the full span
it eventually covers. A homily headed "Rom. I. 8, 9" will wander through verse
17 before it is done. The ranges here are therefore conservative: the entry is
attached to the stated verses plus a modest window, rather than pretending to a
precision the source does not have.
"""
import argparse
import json
import os
import re
import sys
import urllib.request

UA = {"User-Agent": "apparatus-bible-study/0.1"}
CCEL = "https://ccel.org/ccel/schaff/{}/cache/{}.txt"

# volume -> (source_id, title, author, year, tradition, books covered)
VOLUMES = {
    "npnf110": ("chrys_matt", "Chrysostom's Homilies on Matthew",
                "John Chrysostom", "390", "patristic"),
    "npnf111": ("chrys_rom", "Chrysostom's Homilies on Acts and Romans",
                "John Chrysostom", "391", "patristic"),
    "npnf112": ("chrys_cor", "Chrysostom's Homilies on 1 & 2 Corinthians",
                "John Chrysostom", "392", "patristic"),
    "npnf113": ("chrys_gal", "Chrysostom's Homilies on Galatians to Philemon",
                "John Chrysostom", "395", "patristic"),
    "npnf114": ("chrys_john", "Chrysostom's Homilies on John and Hebrews",
                "John Chrysostom", "391", "patristic"),
    "npnf107": ("aug_john", "Augustine's Tractates on the Gospel of John",
                "Augustine of Hippo", "416", "patristic"),
}

# Volumes where the section heading names no book, because the whole volume is
# on one — Augustine's Tractates head each section "Chapter I. 1-5", meaning
# John 1:1-5. Without this the reference parser sees "Chapter" as a book name
# and discards the entire volume.
IMPLIED_BOOK = {"npnf107": "John"}

ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}

# The abbreviations Schaff's editors actually use.
BOOKS = {
    "gen": "Genesis", "ex": "Exodus", "exod": "Exodus", "lev": "Leviticus",
    "num": "Numbers", "deut": "Deuteronomy", "josh": "Joshua",
    "judg": "Judges", "ruth": "Ruth", "sam": "1 Samuel", "kings": "1 Kings",
    "ps": "Psalms", "psalm": "Psalms", "psalms": "Psalms", "prov": "Proverbs",
    "eccl": "Ecclesiastes", "isa": "Isaiah", "jer": "Jeremiah",
    "ezek": "Ezekiel", "dan": "Daniel", "hos": "Hosea", "joel": "Joel",
    "amos": "Amos", "jonah": "Jonah", "mic": "Micah", "hab": "Habakkuk",
    "zech": "Zechariah", "mal": "Malachi",
    "matt": "Matthew", "mat": "Matthew", "mark": "Mark", "luke": "Luke",
    "john": "John", "acts": "Acts", "rom": "Romans",
    "1 cor": "1 Corinthians", "2 cor": "2 Corinthians",
    "gal": "Galatians", "eph": "Ephesians", "phil": "Philippians",
    "philip": "Philippians", "col": "Colossians",
    "1 thess": "1 Thessalonians", "2 thess": "2 Thessalonians",
    "1 tim": "1 Timothy", "2 tim": "2 Timothy", "tit": "Titus",
    "philem": "Philemon", "heb": "Hebrews", "jas": "James",
    "1 pet": "1 Peter", "2 pet": "2 Peter", "1 john": "1 John",
    "jude": "Jude", "rev": "Revelation",
}


def roman_to_int(s: str) -> int | None:
    s = s.strip().lower().rstrip(".")
    if not s or any(c not in ROMAN for c in s):
        return None
    total, prev = 0, 0
    for c in reversed(s):
        v = ROMAN[c]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total or None


# Chrysostom heads his sections "Homily"; Augustine heads his "Tractate".
HOMILY = re.compile(r"^\s*(?:Homily|Tractate)\s+([IVXLC]+)\.?\s*$", re.I)
# "Rom. I. 8, 9"  ·  "1 Cor. xv. 1-3"  ·  "John I. 1"  ·  "Ps. LI."
REF = re.compile(
    r"^\s*((?:[123]\s+)?[A-Za-z]{2,7})\.?\s+"
    r"([IVXLCivxlc]+|\d+)\.?\s*"
    r"([\d,\s\u2013-]*)$"
)


def parse_ref(line: str, implied: str | None = None):
    m = REF.match(line.strip())
    if not m:
        return None
    raw_book, raw_ch, raw_v = m.groups()
    key = re.sub(r"\s+", " ", raw_book.strip().lower())
    book = BOOKS.get(key)
    if not book and implied and key in ("chapter", "chap"):
        book = implied
    if not book:
        return None
    ch = roman_to_int(raw_ch) if not raw_ch.isdigit() else int(raw_ch)
    if not ch:
        return None
    verses = [int(v) for v in re.findall(r"\d+", raw_v or "")]
    if not verses:
        return f"{book} {ch}"
    lo, hi = min(verses), max(verses)
    return f"{book} {ch}:{lo}" if lo == hi else f"{book} {ch}:{lo}-{hi}"


def clean_block(lines: list[str]) -> str:
    text = " ".join(l.strip() for l in lines)
    text = re.sub(r"\[\d+\]", " ", text)          # CCEL footnote markers
    text = re.sub(r"_{5,}", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def convert(vol: str, out_dir: str, cache_dir: str, window: int = 12):
    sid, title, author, year, tradition = VOLUMES[vol]
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{vol}.txt")
    if not os.path.exists(path) or os.path.getsize(path) < 10000:
        print(f"  downloading {vol} ...", flush=True)
        req = urllib.request.Request(CCEL.format(vol, vol), headers=UA)
        with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
            f.write(r.read())

    lines = open(path, encoding="utf-8", errors="replace").read().split("\n")

    entries, i = [], 0
    while i < len(lines):
        if not HOMILY.match(lines[i]):
            i += 1
            continue
        # The reference is the next non-blank line; if it does not parse as a
        # scripture reference the homily is skipped rather than guessed at.
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        ref = parse_ref(lines[j], IMPLIED_BOOK.get(vol)) if j < len(lines) else None
        if not ref:
            i += 1
            continue

        body, k = [], j + 1
        while k < len(lines) and not HOMILY.match(lines[k]):
            body.append(lines[k])
            k += 1
        text = clean_block(body)
        if len(text) > 400:
            # Widen to the window the homily plausibly covers, capped so a
            # single sermon does not claim an entire chapter.
            if ":" in ref and "-" not in ref.split(":")[1]:
                base, v = ref.rsplit(":", 1)
                ref = f"{base}:{v}-{int(v) + window}"
            entries.append({"ref": ref, "text": text})
        i = k

    if not entries:
        print(f"  ! {vol}: no homilies parsed", file=sys.stderr)
        return 0

    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, f"{sid}.jsonl")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(json.dumps({"_source": {
            "id": sid, "title": title, "author": author, "year": year,
            "tradition": tradition, "license": "public domain",
            "url": f"https://ccel.org/ccel/schaff/{vol}",
        }}, ensure_ascii=False) + "\n")
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    chars = sum(len(e["text"]) for e in entries)
    print(f"  {sid:<12} {len(entries):>4} homilies  {chars/1_000_000:>5.1f} MB  [{tradition}]")
    return len(entries)


def main():
    ap = argparse.ArgumentParser(description="Fetch patristic homilies from CCEL")
    ap.add_argument("--out", default="corpora")
    ap.add_argument("--cache", default=".ccelcache")
    ap.add_argument("--volumes", nargs="*", default=None,
                    help=f"default: all. available: {', '.join(VOLUMES)}")
    args = ap.parse_args()

    total = 0
    for v in (args.volumes or list(VOLUMES)):
        if v not in VOLUMES:
            print(f"  ! unknown volume {v}", file=sys.stderr)
            continue
        try:
            total += convert(v, args.out, args.cache)
        except Exception as e:
            print(f"  ! {v} failed: {type(e).__name__}: {e}", file=sys.stderr)

    print(f"\n{total:,} homilies -> {args.out}/")
    print("\nNext:  python -m app.ingest.commentary --dir "
          f"{args.out} --embed")


if __name__ == "__main__":
    main()
