"""
Corpus ingestion: Bible text, cross-references, Strong's lexicon.

    python -m app.ingest.corpus --all

LICENSING — READ THIS BEFORE YOU ADD A TRANSLATION

Bible translations are copyrighted works and the publishers enforce it. This is
the single most common way a well-meaning faith app gets a cease-and-desist.

  Safe to ship, no permission needed:
    BSB    Berean Standard Bible -- dedicated to the public domain in 2023.
           Modern, readable English with no licence to negotiate. This is the
           default reading text and it is the best deal in the whole space.
    WEB    World English Bible, public domain by explicit dedication.
    KJV    public domain (note: still Crown copyright inside the UK)
    ASV    public domain, 1901
    YLT    public domain, extremely literal, useful for word study

  Require a licence or an API agreement:
    ESV    free non-commercial API tier, register at api.esv.org, ~5k calls/day
    NIV    Biblica licensing; commercial terms
    NASB   Lockman Foundation
    CSB    Holman
    NLT    Tyndale House

  api.scripture.api.bible offers a free developer tier fronting many
  translations, with per-translation terms you still have to honour.

SPANISH — READ THIS TWICE

  Reina-Valera 1960 is the version in nearly every Spanish-speaking church, so
  it is the one anyone will name if you ask them. It is COPYRIGHTED by
  Sociedades Bíblicas Unidas and they enforce it. Do not put it in your database
  no matter how many people ask for it. Same for NVI (Biblica) and
  NTV (Tyndale).

  Public domain and safe to ship:
    SpaRV      Reina-Valera 1909. The classic -- roughly what the KJV is to
               English, in stature and in register. This is the default.
    SpaRV1865  Reina-Valera 1865, older orthography.

  If you eventually want RVR1960 alongside these, the route is a licence from
  Sociedades Bíblicas Unidas, not a scraper.

Ship BSB for English and SpaRV for Spanish. Do not scrape a translation off a
website and put it in your database.
"""
import argparse
import io
import json
import re
import sys
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

from .. import refs
from ..db import connect

UA = {"User-Agent": "apparatus-bible-study/0.1 (+github)"}

_SCROLLMAPPER = "https://raw.githubusercontent.com/scrollmapper/bible_databases/master/formats/json/{}.json"

BIBLE_SOURCES = {
    # English
    "BSB": _SCROLLMAPPER.format("BSB"),
    "KJV": _SCROLLMAPPER.format("KJV"),
    "ASV": _SCROLLMAPPER.format("ASV"),
    "YLT": _SCROLLMAPPER.format("YLT"),
    "WEB": None,   # not in that repo; fetched chapter-wise from bible-api.com
    # Spanish
    "SpaRV": _SCROLLMAPPER.format("SpaRV"),          # Reina-Valera 1909
    "SpaRV1865": _SCROLLMAPPER.format("SpaRV1865"),  # Reina-Valera 1865
}

XREF_URL = "https://a.openbible.info/data/cross-references.zip"
HEB_STRONGS = "https://raw.githubusercontent.com/openscriptures/strongs/master/hebrew/StrongHebrewG.xml"
GRK_STRONGS = "https://raw.githubusercontent.com/openscriptures/strongs/master/greek/strongs-greek-dictionary.js"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


# --------------------------------------------------------------------------
# Bible text
# --------------------------------------------------------------------------

def load_bible(con, code: str):
    if code not in BIBLE_SOURCES:
        raise SystemExit(
            f"No public-domain source configured for {code}. Add one only after "
            f"you have verified the licence -- see this module's docstring."
        )
    url = BIBLE_SOURCES[code]
    if url is None:
        return load_bible_via_api(con, code)
    print(f"  fetching {code} ...", flush=True)
    data = json.loads(fetch(url))

    rows, skipped = [], 0
    for book in data["books"]:
        try:
            osis_book = refs._normalize_book(book["name"])
        except refs.RefError:
            skipped += 1
            continue
        for ch in book["chapters"]:
            for v in ch["verses"]:
                text = re.sub(r"\s+", " ", v["text"]).strip()
                if not text:
                    continue
                rows.append((refs.vid(osis_book, ch["chapter"], v["verse"]), code, text))

    con.executemany(
        "INSERT OR REPLACE INTO verses (vid, translation, text) VALUES (?,?,?)", rows
    )
    con.commit()
    print(f"  {code}: {len(rows):,} verses" + (f" ({skipped} books skipped)" if skipped else ""))


def load_bible_via_api(con, code: str):
    """
    Chapter-by-chapter fallback via bible-api.com for public-domain translations
    that have no bulk JSON dump. ~1,190 requests, so it is slow and deliberately
    rate-limited. Prefer a bulk source when one exists.
    """
    import time
    print(f"  fetching {code} chapter-by-chapter (slow) ...", flush=True)
    rows = 0
    # Chapter counts derived from an already-loaded translation, so load KJV first.
    have = con.execute("""
        SELECT vid / 1000 AS chap_key, MAX(vid % 1000) AS last_verse
        FROM verses WHERE translation = (SELECT translation FROM verses LIMIT 1)
        GROUP BY chap_key ORDER BY chap_key
    """).fetchall()
    if not have:
        raise SystemExit(f"Load a bulk translation (e.g. --bibles KJV) before {code}.")

    batch = []
    for r in have:
        book_ord, ch = divmod(r["chap_key"], 1000)
        osis_book = refs.ORD_TO_OSIS[book_ord]
        ref = f"{refs.OSIS_TO_NAME[osis_book]} {ch}"
        try:
            data = json.loads(fetch(
                f"https://bible-api.com/{urllib.parse.quote(ref)}"
                f"?translation={code.lower()}"))
        except Exception:
            continue
        for v in data.get("verses", []):
            text = re.sub(r"\s+", " ", v["text"]).strip()
            if text:
                batch.append((refs.vid(osis_book, v["chapter"], v["verse"]), code, text))
        rows += 1
        if len(batch) >= 2000:
            con.executemany(
                "INSERT OR REPLACE INTO verses (vid, translation, text) VALUES (?,?,?)",
                batch)
            con.commit(); batch.clear()
        time.sleep(0.25)
    if batch:
        con.executemany(
            "INSERT OR REPLACE INTO verses (vid, translation, text) VALUES (?,?,?)", batch)
        con.commit()
    print(f"  {code}: {rows} chapters")


# --------------------------------------------------------------------------
# Cross-references (openbible.info, CC BY)
# --------------------------------------------------------------------------

def load_crossrefs(con, min_votes: int = 0):
    print("  fetching cross-references ...", flush=True)
    blob = fetch(XREF_URL)
    zf = zipfile.ZipFile(io.BytesIO(blob))
    name = next(n for n in zf.namelist() if n.endswith(".txt"))

    rows, bad = [], 0
    with zf.open(name) as fh:
        for i, raw in enumerate(io.TextIOWrapper(fh, encoding="utf-8")):
            if i == 0 or not raw.strip():
                continue
            parts = raw.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            src, dst, votes = parts[0], parts[1], parts[2]
            try:
                from_vid = refs.parse_osis(src)
                if "-" in dst:
                    a, b = dst.split("-", 1)
                    to_start, to_end = refs.parse_osis(a), refs.parse_osis(b)
                else:
                    to_start = to_end = refs.parse_osis(dst)
                votes = int(votes)
            except (refs.RefError, ValueError, KeyError, IndexError):
                bad += 1
                continue
            if votes < min_votes:
                continue
            rows.append((from_vid, to_start, to_end, votes))

    con.execute("DELETE FROM cross_refs")
    con.executemany(
        "INSERT INTO cross_refs (from_vid, to_start, to_end, votes) VALUES (?,?,?,?)",
        rows,
    )
    con.commit()
    print(f"  cross-references: {len(rows):,} links" + (f" ({bad} unparseable)" if bad else ""))


# --------------------------------------------------------------------------
# Strong's lexicon
# --------------------------------------------------------------------------

def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def norm_strongs(sid: str) -> str:
    """
    'H0430' -> 'H430'. The lexicon files, the tagged texts, and every third-party
    dataset disagree about zero-padding, and a mismatch here silently empties the
    word-study panel. Normalise on the way in, always.
    """
    sid = (sid or "").strip().upper()
    if len(sid) > 1 and sid[0] in "HG" and sid[1:].isdigit():
        return sid[0] + str(int(sid[1:]))
    return sid


def load_strongs_hebrew(con):
    print("  fetching Hebrew Strong's ...", flush=True)
    root = ET.fromstring(fetch(HEB_STRONGS).decode("utf-8-sig", "replace"))

    rows = []
    for div in root.iter():
        if _strip_ns(div.tag) != "div" or div.get("type") != "entry":
            continue

        lemma = translit = pron = sid = ""
        senses, explanation, exegesis, translation = [], "", "", ""

        for child in div.iter():
            tag = _strip_ns(child.tag)
            if tag == "w" and child.get("ID") and not sid:
                sid = norm_strongs(child.get("ID"))
                lemma = child.get("lemma") or (child.text or "").strip()
                translit = child.get("xlit") or ""
                pron = child.get("POS") or ""
            elif tag == "item":
                txt = " ".join(child.itertext()).strip()
                if txt:
                    senses.append(txt)
            elif tag == "note":
                txt = " ".join(child.itertext()).strip()
                kind = child.get("type")
                if kind == "explanation":
                    explanation = txt
                elif kind == "exegesis":
                    exegesis = txt
                elif kind == "translation":
                    translation = txt

        if not sid or not sid.startswith("H"):
            continue

        definition = explanation or "; ".join(senses[:6])
        if explanation and senses:
            definition = f"{explanation} — senses: " + "; ".join(senses[:5])

        rows.append((sid, "hebrew", lemma, translit, pron,
                     definition, exegesis, translation))

    _write_strongs(con, rows, "Hebrew")


def load_strongs_greek(con):
    print("  fetching Greek Strong's ...", flush=True)
    raw = fetch(GRK_STRONGS).decode("utf-8", "replace")

    # File is:  var strongsGreekDictionary = {...}; module.exports = ...
    start = raw.find("{", raw.find("strongsGreekDictionary"))
    end = raw.rfind("}", 0, raw.find("module.exports"))
    if start < 0 or end < 0:
        print("  ! Greek lexicon format changed; skipping", file=sys.stderr)
        return
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        print(f"  ! Greek lexicon would not parse: {e}", file=sys.stderr)
        return

    rows = [(
        norm_strongs(sid), "greek",
        e.get("lemma", ""), e.get("translit", ""), "",
        (e.get("strongs_def") or "").strip(),
        (e.get("derivation") or "").strip(),
        (e.get("kjv_def") or "").strip(),
    ) for sid, e in data.items()]
    _write_strongs(con, rows, "Greek")


def _write_strongs(con, rows, label):
    con.executemany("""
        INSERT OR REPLACE INTO strongs
          (id, lang, lemma, translit, pronounce, definition, derivation, kjv_usage)
        VALUES (?,?,?,?,?,?,?,?)
    """, rows)
    con.commit()
    print(f"  {label} Strong's: {len(rows):,} entries")


# --------------------------------------------------------------------------
# Verse -> Strong's mapping
# --------------------------------------------------------------------------

def load_verse_words(con, path: str | None):
    """
    Maps each verse to the original-language words behind it.

    The best open source is STEPBible-Data (github.com/STEPBible/STEPBible-Data,
    CC BY 4.0) -- their TAHOT (Hebrew) and TAGNT (Greek) files give you
    verse ref, original word, transliteration, Strong's number, and gloss, all
    tab-separated. Clone the repo and point --stepbible at the directory.

    Without this, the app still works; you just lose the word-study panel.
    """
    if not path:
        print("  verse_words: skipped (pass --stepbible DIR to enable word study)")
        return

    import glob
    import os

    files = sorted(glob.glob(os.path.join(path, "**", "TA*.txt"), recursive=True))
    if not files:
        print(f"  ! no TAHOT/TAGNT files under {path}", file=sys.stderr)
        return

    rows, current = [], {}
    ref_re = re.compile(r"^([1-3]?\s?[A-Za-z]+)\.(\d+)\.(\d+)")
    strongs_re = re.compile(r"\b([HG]\d{1,5})\b")

    for fp in files:
        with open(fp, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("#") or not line.strip():
                    continue
                cols = line.rstrip("\n").split("\t")
                if len(cols) < 3:
                    continue
                m = ref_re.match(cols[0].strip())
                if not m:
                    continue
                try:
                    book = refs._normalize_book(m.group(1))
                    v = refs.vid(book, int(m.group(2)), int(m.group(3)))
                except (refs.RefError, KeyError, ValueError):
                    continue
                sm = strongs_re.search(line)
                if not sm:
                    continue
                pos = current.get(v, 0)
                current[v] = pos + 1
                surface = cols[1].strip()[:60]
                rows.append((v, pos, norm_strongs(sm.group(1)), surface))

    con.executemany(
        "INSERT OR REPLACE INTO verse_words (vid, position, strongs_id, surface) VALUES (?,?,?,?)",
        rows,
    )
    con.commit()
    print(f"  verse_words: {len(rows):,} tagged words")


def main():
    ap = argparse.ArgumentParser(description="Ingest public-domain scripture data")
    ap.add_argument("--db", default="data/apparatus.db")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--spanish", action="store_true",
                    help="also load Reina-Valera 1909 (public domain)")
    ap.add_argument("--bibles", nargs="*", default=None,
                    help=f"any of {', '.join(BIBLE_SOURCES)} (default: BSB KJV ASV)")
    ap.add_argument("--crossrefs", action="store_true")
    ap.add_argument("--strongs", action="store_true")
    ap.add_argument("--stepbible", default=None,
                    help="path to a clone of STEPBible/STEPBible-Data")
    ap.add_argument("--min-votes", type=int, default=0,
                    help="drop cross-references below this confidence")
    args = ap.parse_args()

    con = connect(args.db)
    print(f"Ingesting into {args.db}")

    if args.all or args.bibles is not None:
        for code in (args.bibles or ["BSB", "KJV", "ASV"]):
            # Spanish codes are mixed-case in the source repo; don't upper() them.
            load_bible(con, code if code in BIBLE_SOURCES else code.upper())
    if args.spanish:
        load_bible(con, "SpaRV")
    if args.all or args.crossrefs:
        load_crossrefs(con, args.min_votes)
    if args.all or args.strongs:
        load_strongs_hebrew(con)
        load_strongs_greek(con)
    if args.all or args.stepbible:
        load_verse_words(con, args.stepbible)

    con.execute("ANALYZE")
    con.commit()
    con.close()
    print("Done.")


if __name__ == "__main__":
    main()
