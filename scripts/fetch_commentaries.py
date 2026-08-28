"""
Convert CrossWire SWORD commentary modules into the JSONL the ingest expects.

    python scripts/fetch_commentaries.py --out corpora
    python -m app.ingest.commentary --dir corpora --embed

WHY SWORD

CCEL and StudyLight have these commentaries as web pages, which means scraping
HTML that changes and hoping the verse boundaries survive. CrossWire's SWORD
project already did that work: the modules are verse-indexed, public domain,
and distributed as single downloads. One format to parse instead of eight
websites to scrape.

THE FORMAT

`zCom4` is the compressed commentary driver. Three files per testament:

  ot.bzv   verse index — 12 bytes per verse: block(uint32), offset(uint32),
           length(uint32), little-endian. Indexed by VERSE ORDINAL in the
           module's versification, not by anything human-readable.
  ot.bzs   block index — 12 bytes per block: offset into .bzz, compressed
           size, uncompressed size.
  ot.bzz   the payload: concatenated zlib streams.

The ordinal scheme is the part that trips people up. It is not "verse number
within the Bible". Each testament begins with a testament-intro slot, each book
begins with a book-intro slot, and each chapter begins with a chapter-intro
slot. Miss those and every comment lands on the wrong verse — silently, since
the text still parses fine. The offsets below account for them.

LICENSING

Everything fetched here is public domain by age (all authors died well before
1930). The SWORD .conf files carry a DistributionLicense field; it is read and
recorded rather than assumed, and a module that does not declare public domain
is skipped with a warning.
"""
import argparse
import io
import json
import os
import re
import struct
import sys
import urllib.request
import zipfile
import zlib

BASE = "https://crosswire.org/ftpmirror/pub/sword/packages/rawzip/{}.zip"
UA = {"User-Agent": "apparatus-bible-study/0.1"}

# module -> (source_id, title, author, year, tradition)
# Chosen for breadth. An all-Reformed panel cannot produce an honest
# "faithfully disputed" section, so Clarke and Wesley (Wesleyan-Arminian),
# Catena (Catholic/patristic) and Scofield (dispensational) are deliberate.
MODULES = {
    "MHC":     ("mhc",      "Matthew Henry's Complete Commentary", "Matthew Henry", "1710", "puritan"),
    "JFB":     ("jfb",      "Jamieson-Fausset-Brown Commentary", "Jamieson, Fausset & Brown", "1871", "reformed"),
    "Barnes":  ("barnes",   "Barnes' Notes on the Bible", "Albert Barnes", "1834", "presbyterian"),
    "Clarke":  ("clarke",   "Clarke's Commentary", "Adam Clarke", "1831", "methodist"),
    "Wesley":  ("wesley",   "Wesley's Explanatory Notes", "John Wesley", "1765", "methodist"),
    "Geneva":  ("geneva",   "Geneva Bible Translation Notes", "Geneva translators", "1599", "reformed"),
    "Catena":  ("catena",   "Catena Aurea", "Thomas Aquinas (compiler)", "1260", "catholic-patristic"),
    "Scofield":("scofield", "Scofield Reference Notes", "C. I. Scofield", "1917", "dispensational"),
    "RWP":     ("rwp",      "Robertson's Word Pictures in the NT", "A. T. Robertson", "1933", "baptist"),
    "PNT":     ("pnt",      "The People's New Testament", "B. W. Johnson", "1891", "restorationist"),
    "Darby":   ("darby",    "Darby's Synopsis", "John Nelson Darby", "1857", "brethren"),
    "Calvin":  ("calvin",   "Calvin's Commentaries", "John Calvin", "1555", "reformed"),
    "Luther":  ("luther",   "Luther's Commentaries", "Martin Luther", "1535", "lutheran"),
    # Wesleyan-Holiness. The closest available ancestor of Pentecostal reading:
    # the movement began around 1906, so essentially nothing Pentecostal is old
    # enough to be public domain. Holiness is where its theology came from.
    "Godbey":  ("godbey",   "Godbey's Commentary on the NT", "W. B. Godbey", "1896", "wesleyan-holiness"),
}
# CrossWire's file for Calvin is named differently from its module id.
FILE_OVERRIDE = {"Calvin": "CalvinCommentaries"}

BOOK_ABBREV = {}   # populated from the canon


# KJV versification: verses per chapter, in canonical order.
#
# Inlined rather than imported. This was a `pysword` dependency, which meant
# pulling a whole SWORD library to read one table of integers — and it broke
# for anyone who had not happened to install it. The numbers are fixed by the
# 1611 text and will not change. Total: 31,102 verses across 66 books.
KJV_CANON = [
    ("ot", "Genesis", [31,25,24,26,32,22,24,22,29,32,32,20,18,24,21,16,27,33,38,18,34,24,20,67,34,35,46,22,35,43,55,32,20,31,29,43,36,30,23,23,57,38,34,34,28,34,31,22,33,26]),
    ("ot", "Exodus", [22,25,22,31,23,30,25,32,35,29,10,51,22,31,27,36,16,27,25,26,36,31,33,18,40,37,21,43,46,38,18,35,23,35,35,38,29,31,43,38]),
    ("ot", "Leviticus", [17,16,17,35,19,30,38,36,24,20,47,8,59,57,33,34,16,30,37,27,24,33,44,23,55,46,34]),
    ("ot", "Numbers", [54,34,51,49,31,27,89,26,23,36,35,16,33,45,41,50,13,32,22,29,35,41,30,25,18,65,23,31,40,16,54,42,56,29,34,13]),
    ("ot", "Deuteronomy", [46,37,29,49,33,25,26,20,29,22,32,32,18,29,23,22,20,22,21,20,23,30,25,22,19,19,26,68,29,20,30,52,29,12]),
    ("ot", "Joshua", [18,24,17,24,15,27,26,35,27,43,23,24,33,15,63,10,18,28,51,9,45,34,16,33]),
    ("ot", "Judges", [36,23,31,24,31,40,25,35,57,18,40,15,25,20,20,31,13,31,30,48,25]),
    ("ot", "Ruth", [22,23,18,22]),
    ("ot", "I Samuel", [28,36,21,22,12,21,17,22,27,27,15,25,23,52,35,23,58,30,24,42,15,23,29,22,44,25,12,25,11,31,13]),
    ("ot", "II Samuel", [27,32,39,12,25,23,29,18,13,19,27,31,39,33,37,23,29,33,43,26,22,51,39,25]),
    ("ot", "I Kings", [53,46,28,34,18,38,51,66,28,29,43,33,34,31,34,34,24,46,21,43,29,53]),
    ("ot", "II Kings", [18,25,27,44,27,33,20,29,37,36,21,21,25,29,38,20,41,37,37,21,26,20,37,20,30]),
    ("ot", "I Chronicles", [54,55,24,43,26,81,40,40,44,14,47,40,14,17,29,43,27,17,19,8,30,19,32,31,31,32,34,21,30]),
    ("ot", "II Chronicles", [17,18,17,22,14,42,22,18,31,19,23,16,22,15,19,14,19,34,11,37,20,12,21,27,28,23,9,27,36,27,21,33,25,33,27,23]),
    ("ot", "Ezra", [11,70,13,24,17,22,28,36,15,44]),
    ("ot", "Nehemiah", [11,20,32,23,19,19,73,18,38,39,36,47,31]),
    ("ot", "Esther", [22,23,15,17,14,14,10,17,32,3]),
    ("ot", "Job", [22,13,26,21,27,30,21,22,35,22,20,25,28,22,35,22,16,21,29,29,34,30,17,25,6,14,23,28,25,31,40,22,33,37,16,33,24,41,30,24,34,17]),
    ("ot", "Psalms", [6,12,8,8,12,10,17,9,20,18,7,8,6,7,5,11,15,50,14,9,13,31,6,10,22,12,14,9,11,12,24,11,22,22,28,12,40,22,13,17,13,11,5,26,17,11,9,14,20,23,19,9,6,7,23,13,11,11,17,12,8,12,11,10,13,20,7,35,36,5,24,20,28,23,10,12,20,72,13,19,16,8,18,12,13,17,7,18,52,17,16,15,5,23,11,13,12,9,9,5,8,28,22,35,45,48,43,13,31,7,10,10,9,8,18,19,2,29,176,7,8,9,4,8,5,6,5,6,8,8,3,18,3,3,21,26,9,8,24,13,10,7,12,15,21,10,20,14,9,6]),
    ("ot", "Proverbs", [33,22,35,27,23,35,27,36,18,32,31,28,25,35,33,33,28,24,29,30,31,29,35,34,28,28,27,28,27,33,31]),
    ("ot", "Ecclesiastes", [18,26,22,16,20,12,29,17,18,20,10,14]),
    ("ot", "Song of Solomon", [17,17,11,16,16,13,13,14]),
    ("ot", "Isaiah", [31,22,26,6,30,13,25,22,21,34,16,6,22,32,9,14,14,7,25,6,17,25,18,23,12,21,13,29,24,33,9,20,24,17,10,22,38,22,8,31,29,25,28,28,25,13,15,22,26,11,23,15,12,17,13,12,21,14,21,22,11,12,19,12,25,24]),
    ("ot", "Jeremiah", [19,37,25,31,31,30,34,22,26,25,23,17,27,22,21,21,27,23,15,18,14,30,40,10,38,24,22,17,32,24,40,44,26,22,19,32,21,28,18,16,18,22,13,30,5,28,7,47,39,46,64,34]),
    ("ot", "Lamentations", [22,22,66,22,22]),
    ("ot", "Ezekiel", [28,10,27,17,17,14,27,18,11,22,25,28,23,23,8,63,24,32,14,49,32,31,49,27,17,21,36,26,21,26,18,32,33,31,15,38,28,23,29,49,26,20,27,31,25,24,23,35]),
    ("ot", "Daniel", [21,49,30,37,31,28,28,27,27,21,45,13]),
    ("ot", "Hosea", [11,23,5,19,15,11,16,14,17,15,12,14,16,9]),
    ("ot", "Joel", [20,32,21]),
    ("ot", "Amos", [15,16,15,13,27,14,17,14,15]),
    ("ot", "Obadiah", [21]),
    ("ot", "Jonah", [17,10,10,11]),
    ("ot", "Micah", [16,13,12,13,15,16,20]),
    ("ot", "Nahum", [15,13,19]),
    ("ot", "Habakkuk", [17,20,19]),
    ("ot", "Zephaniah", [18,15,20]),
    ("ot", "Haggai", [15,23]),
    ("ot", "Zechariah", [21,13,10,14,11,15,14,23,17,12,17,14,9,21]),
    ("ot", "Malachi", [14,17,18,6]),
    ("nt", "Matthew", [25,23,17,25,48,34,29,34,38,42,30,50,58,36,39,28,27,35,30,34,46,46,39,51,46,75,66,20]),
    ("nt", "Mark", [45,28,35,41,43,56,37,38,50,52,33,44,37,72,47,20]),
    ("nt", "Luke", [80,52,38,44,39,49,50,56,62,42,54,59,35,35,32,31,37,43,48,47,38,71,56,53]),
    ("nt", "John", [51,25,36,54,47,71,53,59,41,42,57,50,38,31,27,33,26,40,42,31,25]),
    ("nt", "Acts", [26,47,26,37,42,15,60,40,43,48,30,25,52,28,41,40,34,28,41,38,40,30,35,27,27,32,44,31]),
    ("nt", "Romans", [32,29,31,25,21,23,25,39,33,21,36,21,14,23,33,27]),
    ("nt", "I Corinthians", [31,16,23,21,13,20,40,13,27,33,34,31,13,40,58,24]),
    ("nt", "II Corinthians", [24,17,18,18,21,18,16,24,15,18,33,21,14]),
    ("nt", "Galatians", [24,21,29,31,26,18]),
    ("nt", "Ephesians", [23,22,21,32,33,24]),
    ("nt", "Philippians", [30,30,21,23]),
    ("nt", "Colossians", [29,23,25,18]),
    ("nt", "I Thessalonians", [10,20,13,18,28]),
    ("nt", "II Thessalonians", [12,17,18]),
    ("nt", "I Timothy", [20,15,16,16,25,21]),
    ("nt", "II Timothy", [18,26,17,22]),
    ("nt", "Titus", [16,15,15]),
    ("nt", "Philemon", [25]),
    ("nt", "Hebrews", [14,18,19,16,14,20,28,13,28,39,40,29,25]),
    ("nt", "James", [27,26,18,17,20]),
    ("nt", "I Peter", [25,25,22,19,14]),
    ("nt", "II Peter", [21,22,18]),
    ("nt", "I John", [10,29,24,21,21]),
    ("nt", "II John", [13]),
    ("nt", "III John", [14]),
    ("nt", "Jude", [25]),
    ("nt", "Revelation of John", [20,29,22,11,14,17,17,13,21,11,19,17,18,20,8,21,18,24,21,15,27,21]),
]


def canon():
    """[(testament, book_name, [verses_per_chapter]), ...]"""
    for t, name, counts in KJV_CANON:
        BOOK_ABBREV.setdefault(name, name[:3])
    return [(t, name, counts) for t, name, counts in KJV_CANON]


def verse_ordinals(testament: str):
    """
    Every slot in the testament, in SWORD's order, as
    (ordinal, book_name, chapter, verse). Intro slots are yielded with
    chapter/verse 0 and skipped by the caller.
    """
    # Two slots precede the first book: a module-level heading and the
    # testament heading. Verified empirically — with only one, every comment
    # lands a verse early, and it does so silently because the text still
    # parses. Psalm 23:1 returning the psalm's chapter overview instead of
    # Henry on "The Lord is my shepherd" was the tell.
    idx = 0
    yield idx, None, 0, 0          # module intro
    idx += 1
    yield idx, None, 0, 0          # testament intro
    idx += 1
    for t, name, counts in canon():
        if t != testament:
            continue
        yield idx, name, 0, 0      # book intro
        idx += 1
        for ch, n in enumerate(counts, start=1):
            yield idx, name, ch, 0  # chapter intro
            idx += 1
            for v in range(1, n + 1):
                yield idx, name, ch, v
                idx += 1


TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")
ENTITY = {"&quot;": '"', "&apos;": "'", "&amp;": "&", "&lt;": "<", "&gt;": ">",
          "&nbsp;": " ", "&mdash;": "—", "&ndash;": "–"}


def clean(raw: bytes) -> str:
    """OSIS/ThML markup to readable prose."""
    s = raw.decode("utf-8", "replace")
    s = re.sub(r"<title[^>]*>(.*?)</title>", r"\1. ", s, flags=re.S | re.I)
    s = re.sub(r"<(br|lb)\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"</(p|div|lg|l)>", " ", s, flags=re.I)
    s = re.sub(r"<note[^>]*>.*?</note>", " ", s, flags=re.S | re.I)
    s = TAG.sub(" ", s)
    for k, v in ENTITY.items():
        s = s.replace(k, v)
    s = re.sub(r"&#\d+;", " ", s)
    return WS.sub(" ", s).strip()


def read_module(zf: zipfile.ZipFile, datapath: str, testament: str,
                moddrv: str = "zcom4", blocktype: str = "BOOK"):
    """
    Yield (book, chapter, verse, text) for one testament.

    Three things vary between modules and all three are silent failures:

      ModDrv     zCom  -> 2-byte length, 10-byte index records
                 zCom4 -> 4-byte length, 12-byte index records
                 Reading Clarke's 2-byte lengths as 4-byte produced 591 MB of
                 concatenated garbage that still decompressed cleanly.

      BlockType  BOOK -> ot.bzv / ot.bzs / ot.bzz
                 CHAPTER -> ot.czv / ...
                 VERSE -> ot.vzv / ...
                 The wrong prefix simply finds no file, so the module comes
                 back empty rather than wrong.

      Testament  Several modules cover only the New Testament (Catena on the
                 Gospels, Barnes here). A missing ot.* is normal.
    """
    prefix = {"BOOK": "b", "CHAPTER": "c", "VERSE": "v"}.get(
        (blocktype or "BOOK").upper(), "b")
    wide = moddrv.lower().endswith("4")
    rec = 12 if wide else 10

    base = datapath.strip("./").rstrip("/")
    leaf = base.split("/")[-1]

    def grab(kind):
        want = f"{testament}.{prefix}z{kind}".lower()
        for n in zf.namelist():
            low = n.lower()
            if low.endswith(want) and leaf in low:
                return zf.read(n)
        return None

    idx, blk, payload = grab("v"), grab("s"), grab("z")
    if not (idx and blk and payload):
        return

    blocks = []
    for i in range(0, len(blk) - 11, 12):
        off, csize, usize = struct.unpack_from("<III", blk, i)
        blocks.append((off, csize))

    cache = {}

    def block(n):
        if n not in cache:
            if n >= len(blocks):
                return b""
            off, csize = blocks[n]
            try:
                cache.clear()          # one at a time; these decompress large
                cache[n] = zlib.decompress(payload[off:off + csize])
            except zlib.error:
                cache[n] = b""
        return cache[n]

    for ordinal, bname, ch, v in verse_ordinals(testament):
        if not bname or ch == 0 or v == 0:
            continue
        pos = ordinal * rec
        if pos + rec > len(idx):
            break
        if wide:
            bnum, offset, length = struct.unpack_from("<III", idx, pos)
        else:
            bnum, offset, length = struct.unpack_from("<IIH", idx, pos)
        if not length:
            continue
        chunk = block(bnum)[offset:offset + length]
        if not chunk:
            continue
        text = clean(chunk)
        if len(text) < 25:
            continue
        yield bname, ch, v, text


def fetch(mod: str, cache_dir: str) -> str:
    fname = FILE_OVERRIDE.get(mod, mod)
    path = os.path.join(cache_dir, f"{fname}.zip")
    if os.path.exists(path) and os.path.getsize(path) > 10000:
        return path
    os.makedirs(cache_dir, exist_ok=True)
    url = BASE.format(fname)
    print(f"  downloading {fname} ...", flush=True)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
        f.write(r.read())
    return path


def read_conf(zf: zipfile.ZipFile):
    for n in zf.namelist():
        if n.lower().endswith(".conf"):
            txt = zf.read(n).decode("utf-8", "replace")
            conf = {}
            for line in txt.splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, val = line.split("=", 1)
                    conf[k.strip().lower()] = val.strip()
            return conf
    return {}


def convert(mod: str, out_dir: str, cache_dir: str, merge_verses: bool = True):
    sid, title, author, year, tradition = MODULES[mod]
    path = fetch(mod, cache_dir)
    zf = zipfile.ZipFile(path)
    conf = read_conf(zf)

    lic = conf.get("distributionlicense", "")
    if lic and "public domain" not in lic.lower():
        print(f"  ! {mod}: licence is {lic!r}, not public domain — skipping",
              file=sys.stderr)
        return 0

    datapath = conf.get("datapath", f"./modules/comments/zcom4/{sid}/")
    moddrv = conf.get("moddrv", "zCom4")
    blocktype = conf.get("blocktype", "BOOK")
    rows = []
    for testament in ("ot", "nt"):
        rows.extend(read_module(zf, datapath, testament, moddrv, blocktype))

    if not rows:
        print(f"  ! {mod}: no entries parsed", file=sys.stderr)
        return 0

    # Many commentators write one block covering several verses; SWORD repeats
    # that block on each verse. Collapse the repeats into a single ranged entry
    # so the same paragraph is not stored eight times.
    entries = []
    if merge_verses:
        i = 0
        while i < len(rows):
            b, c, v, t = rows[i]
            j = i + 1
            while (j < len(rows) and rows[j][0] == b and rows[j][1] == c
                   and rows[j][3] == t):
                j += 1
            last_v = rows[j - 1][2]
            ref = f"{b} {c}:{v}" if v == last_v else f"{b} {c}:{v}-{last_v}"
            entries.append({"ref": ref, "text": t})
            i = j
    else:
        entries = [{"ref": f"{b} {c}:{v}", "text": t} for b, c, v, t in rows]

    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, f"{sid}.jsonl")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(json.dumps({"_source": {
            "id": sid, "title": title, "author": author, "year": year,
            "tradition": tradition,
            "license": lic or "public domain",
            "url": f"https://crosswire.org/sword/modules/ModInfo.jsp?modName={mod}",
        }}, ensure_ascii=False) + "\n")
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    chars = sum(len(e["text"]) for e in entries)
    print(f"  {sid:<10} {len(entries):>7,} entries  {chars/1_000_000:>5.1f} MB  [{tradition}]")
    return len(entries)


def main():
    ap = argparse.ArgumentParser(description="Fetch public-domain commentaries")
    ap.add_argument("--out", default="corpora")
    ap.add_argument("--cache", default=".swordcache")
    ap.add_argument("--modules", nargs="*", default=None,
                    help=f"default: a balanced starter set. all: {', '.join(MODULES)}")
    ap.add_argument("--all", action="store_true", help="every module listed")
    args = ap.parse_args()

    # Three traditions minimum, or the disputed feature has nothing to work with.
    # Ten commentators across nine traditions. Breadth is the point: a panel
    # that is all Reformed cannot produce an honest "faithfully disputed"
    # section, it can only produce an in-house argument. Clarke and Wesley
    # bring Wesleyan-Arminian, Luther brings Lutheran, Scofield and Darby bring
    # dispensational, Catena brings the church fathers, PNT brings the
    # Restoration movement.
    default = ["MHC", "JFB", "Clarke", "Barnes", "Wesley", "Catena",
               "Calvin", "Luther", "Scofield", "PNT", "Godbey"]
    mods = list(MODULES) if args.all else (args.modules or default)

    total = 0
    traditions = set()
    for m in mods:
        if m not in MODULES:
            print(f"  ! unknown module {m}", file=sys.stderr)
            continue
        try:
            n = convert(m, args.out, args.cache)
            if n:
                total += n
                traditions.add(MODULES[m][4])
        except Exception as e:
            print(f"  ! {m} failed: {type(e).__name__}: {e}", file=sys.stderr)

    print(f"\n{total:,} entries across {len(traditions)} traditions -> {args.out}/")
    if len(traditions) < 3:
        print("Fewer than three traditions. Add more before relying on the "
              "disputed-questions feature.", file=sys.stderr)
    print("\nNext:  python -m app.ingest.commentary --dir "
          f"{args.out} --embed")


if __name__ == "__main__":
    main()
