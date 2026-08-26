"""
Canonical reference handling.

Every verse gets an integer key: book_ordinal * 1_000_000 + chapter * 1_000 + verse.
Integer keys make range overlap queries trivial in SQL, which is the whole reason
we bother. "Rom.5.1" -> 45005001.
"""
import re
import unicodedata

BOOKS = [
    ("Gen", "Genesis", ["ge", "gen", "genesis"]),
    ("Exod", "Exodus", ["ex", "exo", "exod", "exodus"]),
    ("Lev", "Leviticus", ["le", "lev", "leviticus"]),
    ("Num", "Numbers", ["nu", "num", "numbers"]),
    ("Deut", "Deuteronomy", ["de", "deu", "deut", "dt", "deuteronomy"]),
    ("Josh", "Joshua", ["jos", "josh", "joshua"]),
    ("Judg", "Judges", ["jdg", "judg", "judges"]),
    ("Ruth", "Ruth", ["ru", "rut", "ruth"]),
    ("1Sam", "1 Samuel", ["1sa", "1sam", "1samuel", "isam"]),
    ("2Sam", "2 Samuel", ["2sa", "2sam", "2samuel", "iisam"]),
    ("1Kgs", "1 Kings", ["1ki", "1kg", "1kgs", "1kings"]),
    ("2Kgs", "2 Kings", ["2ki", "2kg", "2kgs", "2kings"]),
    ("1Chr", "1 Chronicles", ["1ch", "1chr", "1chronicles"]),
    ("2Chr", "2 Chronicles", ["2ch", "2chr", "2chronicles"]),
    ("Ezra", "Ezra", ["ezr", "ezra"]),
    ("Neh", "Nehemiah", ["ne", "neh", "nehemiah"]),
    ("Esth", "Esther", ["es", "est", "esth", "esther"]),
    ("Job", "Job", ["job"]),
    ("Ps", "Psalms", ["ps", "psa", "psalm", "psalms"]),
    ("Prov", "Proverbs", ["pr", "pro", "prov", "proverbs"]),
    ("Eccl", "Ecclesiastes", ["ec", "ecc", "eccl", "ecclesiastes"]),
    ("Song", "Song of Solomon", ["so", "song", "sos", "canticles"]),
    ("Isa", "Isaiah", ["isa", "is", "isaiah"]),
    ("Jer", "Jeremiah", ["jer", "je", "jeremiah"]),
    ("Lam", "Lamentations", ["la", "lam", "lamentations"]),
    ("Ezek", "Ezekiel", ["eze", "ezek", "ezekiel"]),
    ("Dan", "Daniel", ["da", "dan", "daniel"]),
    ("Hos", "Hosea", ["ho", "hos", "hosea"]),
    ("Joel", "Joel", ["joe", "joel"]),
    ("Amos", "Amos", ["am", "amo", "amos"]),
    ("Obad", "Obadiah", ["ob", "oba", "obad", "obadiah"]),
    ("Jonah", "Jonah", ["jon", "jonah"]),
    ("Mic", "Micah", ["mic", "mi", "micah"]),
    ("Nah", "Nahum", ["na", "nah", "nahum"]),
    ("Hab", "Habakkuk", ["hab", "habakkuk"]),
    ("Zeph", "Zephaniah", ["zep", "zeph", "zephaniah"]),
    ("Hag", "Haggai", ["hag", "haggai"]),
    ("Zech", "Zechariah", ["zec", "zech", "zechariah"]),
    ("Mal", "Malachi", ["mal", "malachi"]),
    ("Matt", "Matthew", ["mt", "mat", "matt", "matthew"]),
    ("Mark", "Mark", ["mk", "mar", "mark"]),
    ("Luke", "Luke", ["lk", "luk", "luke"]),
    ("John", "John", ["jn", "joh", "john"]),
    ("Acts", "Acts", ["ac", "act", "acts"]),
    ("Rom", "Romans", ["ro", "rom", "romans"]),
    ("1Cor", "1 Corinthians", ["1co", "1cor", "1corinthians"]),
    ("2Cor", "2 Corinthians", ["2co", "2cor", "2corinthians"]),
    ("Gal", "Galatians", ["ga", "gal", "galatians"]),
    ("Eph", "Ephesians", ["eph", "ephesians"]),
    ("Phil", "Philippians", ["php", "phil", "philippians"]),
    ("Col", "Colossians", ["col", "colossians"]),
    ("1Thess", "1 Thessalonians", ["1th", "1thess", "1thessalonians"]),
    ("2Thess", "2 Thessalonians", ["2th", "2thess", "2thessalonians"]),
    ("1Tim", "1 Timothy", ["1ti", "1tim", "1timothy"]),
    ("2Tim", "2 Timothy", ["2ti", "2tim", "2timothy"]),
    ("Titus", "Titus", ["tit", "titus"]),
    ("Phlm", "Philemon", ["phm", "phlm", "philemon"]),
    ("Heb", "Hebrews", ["heb", "hebrews"]),
    ("Jas", "James", ["jas", "jam", "james"]),
    ("1Pet", "1 Peter", ["1pe", "1pet", "1peter"]),
    ("2Pet", "2 Peter", ["2pe", "2pet", "2peter"]),
    ("1John", "1 John", ["1jn", "1joh", "1john"]),
    ("2John", "2 John", ["2jn", "2joh", "2john"]),
    ("3John", "3 John", ["3jn", "3joh", "3john"]),
    ("Jude", "Jude", ["jud", "jude"]),
    ("Rev", "Revelation", ["re", "rev", "revelation", "apocalypse"]),
]

OSIS_TO_ORD = {b[0]: i + 1 for i, b in enumerate(BOOKS)}
ORD_TO_OSIS = {i + 1: b[0] for i, b in enumerate(BOOKS)}
OSIS_TO_NAME = {b[0]: b[1] for b in BOOKS}

def _strip_accents(s: str) -> str:
    """'Génesis' -> 'genesis'. People type accents inconsistently, and phone
    keyboards fight them, so match without."""
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


_ALIAS = {}
for _osis, _name, _aliases in BOOKS:
    _ALIAS[_osis.lower()] = _osis
    _ALIAS[_name.lower().replace(" ", "")] = _osis
    for a in _aliases:
        _ALIAS[a] = _osis



# Spanish book names, mapped to the same OSIS codes. Accents are stripped before
# lookup, so both "Génesis" and "Genesis" resolve. Includes the abbreviations
# printed in Spanish Bibles (Gn, Éx, Lv, Mt, Mr, Jn, Hch, Ro, Ap...), because
# those are what people actually type.
SPANISH_BOOKS = {
    "Gen": ["genesis", "gn", "ge"],
    "Exod": ["exodo", "ex", "exo"],
    "Lev": ["levitico", "lv", "lev"],
    "Num": ["numeros", "nm", "num"],
    "Deut": ["deuteronomio", "dt", "deut"],
    "Josh": ["josue", "jos"],
    "Judg": ["jueces", "jue", "jc"],
    "Ruth": ["rut", "rt"],
    "1Sam": ["1samuel", "1sm", "1s"],
    "2Sam": ["2samuel", "2sm", "2s"],
    "1Kgs": ["1reyes", "1re", "1r"],
    "2Kgs": ["2reyes", "2re", "2r"],
    "1Chr": ["1cronicas", "1cr"],
    "2Chr": ["2cronicas", "2cr"],
    "Ezra": ["esdras", "esd"],
    "Neh": ["nehemias", "neh", "ne"],
    "Esth": ["ester", "est"],
    "Job": ["job", "jb"],
    "Ps": ["salmos", "salmo", "sal", "sl"],
    "Prov": ["proverbios", "proverbio", "pr", "prov"],
    "Eccl": ["eclesiastes", "ec", "ecl"],
    "Song": ["cantares", "cantardeloscantares", "cnt", "cant"],
    "Isa": ["isaias", "is", "isa"],
    "Jer": ["jeremias", "jer", "jr"],
    "Lam": ["lamentaciones", "lm", "lam"],
    "Ezek": ["ezequiel", "ez", "eze"],
    "Dan": ["daniel", "dn", "dan"],
    "Hos": ["oseas", "os"],
    "Joel": ["joel", "jl"],
    "Amos": ["amos", "am"],
    "Obad": ["abdias", "abd", "ab"],
    "Jonah": ["jonas", "jon"],
    "Mic": ["miqueas", "miq", "mi"],
    "Nah": ["nahum", "nah", "na"],
    "Hab": ["habacuc", "hab"],
    "Zeph": ["sofonias", "sof"],
    "Hag": ["hageo", "hag", "ag"],
    "Zech": ["zacarias", "zac", "za"],
    "Mal": ["malaquias", "mal"],
    "Matt": ["mateo", "mt", "mat"],
    "Mark": ["marcos", "mr", "mrc", "mc"],
    "Luke": ["lucas", "lc", "luc"],
    "John": ["juan", "jn", "jua"],
    "Acts": ["hechos", "hch", "hec", "hh"],
    "Rom": ["romanos", "ro", "rom", "rm"],
    "1Cor": ["1corintios", "1co", "1cor"],
    "2Cor": ["2corintios", "2co", "2cor"],
    "Gal": ["galatas", "ga", "gal"],
    "Eph": ["efesios", "ef", "efe"],
    "Phil": ["filipenses", "fil", "flp"],
    "Col": ["colosenses", "col"],
    "1Thess": ["1tesalonicenses", "1ts", "1tes"],
    "2Thess": ["2tesalonicenses", "2ts", "2tes"],
    "1Tim": ["1timoteo", "1ti", "1tim"],
    "2Tim": ["2timoteo", "2ti", "2tim"],
    "Titus": ["tito", "tit"],
    "Phlm": ["filemon", "flm"],
    "Heb": ["hebreos", "he", "heb"],
    "Jas": ["santiago", "stg", "sant", "st"],
    "1Pet": ["1pedro", "1pe", "1p"],
    "2Pet": ["2pedro", "2pe", "2p"],
    "1John": ["1juan", "1jn"],
    "2John": ["2juan", "2jn"],
    "3John": ["3juan", "3jn"],
    "Jude": ["judas", "jud", "jds"],
    "Rev": ["apocalipsis", "ap", "apoc"],
}

# Display names per UI language.
BOOK_NAMES = {
    "en": OSIS_TO_NAME,
    "es": {
        "Gen": "Génesis", "Exod": "Éxodo", "Lev": "Levítico", "Num": "Números",
        "Deut": "Deuteronomio", "Josh": "Josué", "Judg": "Jueces", "Ruth": "Rut",
        "1Sam": "1 Samuel", "2Sam": "2 Samuel", "1Kgs": "1 Reyes", "2Kgs": "2 Reyes",
        "1Chr": "1 Crónicas", "2Chr": "2 Crónicas", "Ezra": "Esdras",
        "Neh": "Nehemías", "Esth": "Ester", "Job": "Job", "Ps": "Salmos",
        "Prov": "Proverbios", "Eccl": "Eclesiastés", "Song": "Cantares",
        "Isa": "Isaías", "Jer": "Jeremías", "Lam": "Lamentaciones",
        "Ezek": "Ezequiel", "Dan": "Daniel", "Hos": "Oseas", "Joel": "Joel",
        "Amos": "Amós", "Obad": "Abdías", "Jonah": "Jonás", "Mic": "Miqueas",
        "Nah": "Nahúm", "Hab": "Habacuc", "Zeph": "Sofonías", "Hag": "Hageo",
        "Zech": "Zacarías", "Mal": "Malaquías", "Matt": "Mateo", "Mark": "Marcos",
        "Luke": "Lucas", "John": "Juan", "Acts": "Hechos", "Rom": "Romanos",
        "1Cor": "1 Corintios", "2Cor": "2 Corintios", "Gal": "Gálatas",
        "Eph": "Efesios", "Phil": "Filipenses", "Col": "Colosenses",
        "1Thess": "1 Tesalonicenses", "2Thess": "2 Tesalonicenses",
        "1Tim": "1 Timoteo", "2Tim": "2 Timoteo", "Titus": "Tito",
        "Phlm": "Filemón", "Heb": "Hebreos", "Jas": "Santiago",
        "1Pet": "1 Pedro", "2Pet": "2 Pedro", "1John": "1 Juan",
        "2John": "2 Juan", "3John": "3 Juan", "Jude": "Judas",
        "Rev": "Apocalipsis",
    },
}

for _osis, _als in SPANISH_BOOKS.items():
    for _a in _als:
        _ALIAS.setdefault(_strip_accents(_a), _osis)
for _osis, _name in BOOK_NAMES["es"].items():
    _ALIAS.setdefault(_strip_accents(_name.lower().replace(" ", "")), _osis)


# \w with re.UNICODE matches accented letters, so "Génesis" and "Éxodo" parse.
# Digits are excluded from the body so "Juan 3" splits book from chapter.
# OSIS / STEPBible abbreviations. Several differ from the common English
# shorthand — "Jhn" not "Jn", "Ezk" not "Eze", "Php" not "Phil" — and a miss
# here silently drops whole books during interlinear ingest.
OSIS_ABBREV = {
    "gen": "Gen", "exo": "Exod", "lev": "Lev", "num": "Num", "deu": "Deut",
    "jos": "Josh", "jdg": "Judg", "rut": "Ruth", "1sa": "1Sam", "2sa": "2Sam",
    "1ki": "1Kgs", "2ki": "2Kgs", "1ch": "1Chr", "2ch": "2Chr", "ezr": "Ezra",
    "neh": "Neh", "est": "Esth", "job": "Job", "psa": "Ps", "pro": "Prov",
    "ecc": "Eccl", "sng": "Song", "isa": "Isa", "jer": "Jer", "lam": "Lam",
    "ezk": "Ezek", "dan": "Dan", "hos": "Hos", "jol": "Joel", "amo": "Amos",
    "oba": "Obad", "jon": "Jonah", "mic": "Mic", "nam": "Nah", "hab": "Hab",
    "zep": "Zeph", "hag": "Hag", "zec": "Zech", "mal": "Mal",
    "mat": "Matt", "mrk": "Mark", "luk": "Luke", "jhn": "John", "act": "Acts",
    "rom": "Rom", "1co": "1Cor", "2co": "2Cor", "gal": "Gal", "eph": "Eph",
    "php": "Phil", "col": "Col", "1th": "1Thess", "2th": "2Thess",
    "1ti": "1Tim", "2ti": "2Tim", "tit": "Titus", "phm": "Phlm", "heb": "Heb",
    "jas": "Jas", "1pe": "1Pet", "2pe": "2Pet", "1jn": "1John", "2jn": "2John",
    "3jn": "3John", "jud": "Jude", "rev": "Rev",
}
for _a, _o in OSIS_ABBREV.items():
    _ALIAS.setdefault(_a, _o)


_REF_RE = re.compile(
    r"^\s*(?P<book>(?:[1-3]\s*)?[^\W\d_][^\W\d_\s]*(?:\s+[^\W\d_][^\W\d_\s]*)*?)\s*"
    r"(?P<chapter>\d+)"
    r"(?:\s*[:.]\s*(?P<verse>\d+)(?:\s*[-–]\s*(?P<end>\d+))?)?\s*$"
)

# Roman-numeral and word prefixes people actually type
_PREFIX_FIX = {
    "first": "1", "second": "2", "third": "3",
    "i": "1", "ii": "2", "iii": "3",
}


class RefError(ValueError):
    pass


def _normalize_book(raw: str) -> str:
    s = _strip_accents(raw.lower()).replace(".", "").strip()
    parts = s.split()
    if parts and parts[0] in _PREFIX_FIX:
        parts[0] = _PREFIX_FIX[parts[0]]
    s = "".join(parts)
    if s in _ALIAS:
        return _ALIAS[s]
    # tolerate "1corinthians" vs "1cor" style truncations
    for alias, osis in _ALIAS.items():
        if s.startswith(alias) and len(alias) >= 3:
            return osis
    raise RefError(f"Unknown book: {raw!r}")


def vid(osis_book: str, chapter: int, verse: int) -> int:
    """Verse id as sortable integer."""
    return OSIS_TO_ORD[osis_book] * 1_000_000 + chapter * 1_000 + verse


def unvid(n: int) -> tuple[str, int, int]:
    book = ORD_TO_OSIS[n // 1_000_000]
    rest = n % 1_000_000
    return book, rest // 1_000, rest % 1_000


def osis(n: int) -> str:
    b, c, v = unvid(n)
    return f"{b}.{c}.{v}"


def book_name(osis_book: str, lang: str = "en") -> str:
    return BOOK_NAMES.get(lang, OSIS_TO_NAME).get(osis_book, OSIS_TO_NAME[osis_book])


def label(n: int, lang: str = "en") -> str:
    b, c, v = unvid(n)
    return f"{book_name(b, lang)} {c}:{v}"


def range_label(start: int, end: int, lang: str = "en") -> str:
    if start == end:
        return label(start, lang)
    b1, c1, v1 = unvid(start)
    b2, c2, v2 = unvid(end)
    if b1 == b2 and c1 == c2:
        if v1 == 1 and v2 >= 999:          # whole-chapter sentinel
            return f"{book_name(b1, lang)} {c1}"
        return f"{book_name(b1, lang)} {c1}:{v1}-{v2}"
    return f"{label(start, lang)}–{label(end, lang)}"


def parse(ref: str) -> tuple[int, int]:
    """
    'John 3:16'      -> (43003016, 43003016)
    'Romans 5:1-5'   -> (45005001, 45005005)
    '1 Cor 13'       -> whole chapter
    Returns inclusive (start_vid, end_vid).
    """
    m = _REF_RE.match(ref)
    if not m:
        raise RefError(f"Could not parse reference: {ref!r}")
    book = _normalize_book(m.group("book"))
    ch = int(m.group("chapter"))
    if m.group("verse") is None:
        return vid(book, ch, 1), vid(book, ch, 999)
    v = int(m.group("verse"))
    end = int(m.group("end")) if m.group("end") else v
    if end < v:
        raise RefError("Range ends before it starts")
    return vid(book, ch, v), vid(book, ch, end)


def parse_osis(s: str) -> int:
    """'Gen.1.1' -> int. Tolerates 'Gen.1' (verse 1)."""
    parts = s.split(".")
    book = _normalize_book(parts[0])
    ch = int(parts[1]) if len(parts) > 1 else 1
    v = int(parts[2]) if len(parts) > 2 else 1
    return vid(book, ch, v)
