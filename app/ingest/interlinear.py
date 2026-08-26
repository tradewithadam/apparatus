"""
Interlinear ingest — the original-language text, word by word.

    git clone --depth 1 https://github.com/STEPBible/STEPBible-Data.git
    python -m app.ingest.interlinear --stepbible STEPBible-Data

STEPBible's TAHOT (Hebrew) and TAGNT (Greek) files are CC BY 4.0 and give,
per word: the original form, a transliteration, a contextual English gloss, a
Strong's number, and a morphology code. The Greek files carry a Spanish gloss
too, which the Spanish mode uses.

THE TWO FILE FORMATS DIFFER, which is the only real trap here:

  Greek   Jhn.3.16#01=NKO | οὕτως (houtōs) | Thus | G3779=ADV | ...
                            word+translit    gloss  strongs=morph
          ...and a Spanish gloss around column 8.

  Hebrew  Gen.1.1#01=L | בְּ/רֵאשִׁ֖ית | be./re.Shit | in/ beginning | H9003/{H7225G} | HR/Ncfsa
                          word           translit      gloss          strongs         morph

Same repository, same project, different column layout. Detected by filename.

Hebrew words are often compounds — בְּ/רֵאשִׁית is preposition + noun, marked
with a slash. The slash is preserved in the display form because it shows the
reader something true about how Hebrew builds words.
"""
import argparse
import glob
import os
import re
import sys

from .. import refs
from ..db import connect
from .corpus import norm_strongs

# --------------------------------------------------------------------------
# Morphology, decoded into something a non-specialist can read.
# Not exhaustive; covers the codes that carry most of the meaning.
# --------------------------------------------------------------------------

GK_TENSE = {"P": "present", "I": "imperfect", "F": "future", "A": "aorist",
            "X": "perfect", "Y": "pluperfect", "R": "perfect"}
GK_VOICE = {"A": "active", "M": "middle", "P": "passive", "E": "middle/passive"}
GK_MOOD = {"I": "indicative", "S": "subjunctive", "O": "optative",
           "M": "imperative", "N": "infinitive", "P": "participle"}
GK_CASE = {"N": "nominative", "G": "genitive", "D": "dative",
           "A": "accusative", "V": "vocative"}
GK_NUM = {"S": "singular", "P": "plural"}
GK_GENDER = {"M": "masculine", "F": "feminine", "N": "neuter"}

HB_POS = {"V": "verb", "N": "noun", "A": "adjective", "P": "pronoun",
          "R": "preposition", "C": "conjunction", "D": "adverb",
          "T": "particle", "S": "suffix"}
HB_STEM = {"q": "qal", "N": "niphal", "p": "piel", "P": "pual",
           "h": "hiphil", "H": "hophal", "t": "hithpael"}
HB_ASPECT = {"p": "perfect (completed)", "i": "imperfect (ongoing)",
             "w": "consecutive", "v": "imperative", "a": "infinitive absolute",
             "c": "infinitive construct", "r": "participle", "s": "passive participle"}


def decode_greek(code: str) -> str:
    """'V-AAI-3S' -> 'verb · aorist active indicative · 3rd person singular'."""
    if not code:
        return ""
    parts = code.replace("=", "-").split("-")
    head = parts[0].upper()
    out = []
    if head.startswith("V") and len(parts) > 1:
        t = parts[1]
        bits = []
        if len(t) > 0 and t[0] in GK_TENSE: bits.append(GK_TENSE[t[0]])
        if len(t) > 1 and t[1] in GK_VOICE: bits.append(GK_VOICE[t[1]])
        if len(t) > 2 and t[2] in GK_MOOD:  bits.append(GK_MOOD[t[2]])
        out.append("verb")
        if bits:
            out.append(" ".join(bits))
        if len(parts) > 2:
            p = parts[2]
            person = p[0] if p and p[0].isdigit() else ""
            num = GK_NUM.get(p[-1].upper(), "") if p else ""
            if person or num:
                suffix = {"1": "1st person", "2": "2nd person", "3": "3rd person"}.get(person, "")
                out.append(" ".join(x for x in (suffix, num) if x))
    elif head in ("N", "A", "T", "P", "D", "R"):
        label = {"N": "noun", "A": "adjective", "T": "article",
                 "P": "pronoun", "D": "demonstrative", "R": "relative"}[head]
        out.append(label)
        if len(parts) > 1:
            g = parts[1]
            bits = [GK_CASE.get(g[0].upper(), "") if len(g) > 0 else "",
                    GK_NUM.get(g[1].upper(), "") if len(g) > 1 else "",
                    GK_GENDER.get(g[2].upper(), "") if len(g) > 2 else ""]
            bits = [b for b in bits if b]
            if bits:
                out.append(" ".join(bits))
    elif head == "ADV":
        out.append("adverb")
    elif head == "CONJ":
        out.append("conjunction")
    elif head == "PREP":
        out.append("preposition")
    return " · ".join(out)


def decode_hebrew(code: str) -> str:
    """'HVqp3ms' -> 'verb · qal · perfect (completed) · 3rd person masculine singular'."""
    if not code:
        return ""
    c = code[1:] if code[:1] == "H" else code
    c = c.split("/")[0]
    if not c:
        return ""
    out = []
    pos = HB_POS.get(c[0].upper() if c[0].upper() in HB_POS else c[0], "")
    if c[:1] == "V":
        out.append("verb")
        if len(c) > 1 and c[1] in HB_STEM: out.append(HB_STEM[c[1]])
        if len(c) > 2 and c[2] in HB_ASPECT: out.append(HB_ASPECT[c[2]])
        tail = c[3:]
        m = re.match(r"(\d)?([mfc])?([sp])?", tail)
        if m:
            person = {"1": "1st person", "2": "2nd person", "3": "3rd person"}.get(m.group(1) or "", "")
            gender = {"m": "masculine", "f": "feminine", "c": "common"}.get(m.group(2) or "", "")
            num = {"s": "singular", "p": "plural"}.get(m.group(3) or "", "")
            bits = [x for x in (person, gender, num) if x]
            if bits:
                out.append(" ".join(bits))
    elif pos:
        out.append(pos)
        m = re.search(r"([mf])([sp])", c)
        if m:
            out.append({"m": "masculine", "f": "feminine"}[m.group(1)] + " " +
                       {"s": "singular", "p": "plural"}[m.group(2)])
    return " · ".join(out)


REF_RE = re.compile(r"^([1-3]?[A-Za-z]+)\.(\d+)\.(\d+)#(\d+)")
STRONGS_RE = re.compile(r"([HG]\d{3,5})")


def parse_line(line: str, hebrew: bool):
    cols = line.rstrip("\n").split("\t")
    if len(cols) < 4:
        return None
    m = REF_RE.match(cols[0].strip())
    if not m:
        return None
    try:
        book = refs._normalize_book(m.group(1))
        vid = refs.vid(book, int(m.group(2)), int(m.group(3)))
    except (refs.RefError, KeyError, ValueError):
        return None
    pos = int(m.group(4))

    if hebrew:
        original = cols[1].strip()
        translit = cols[2].strip()
        gloss = cols[3].strip()
        raw_strongs = cols[4] if len(cols) > 4 else ""
        morph_raw = cols[5].strip() if len(cols) > 5 else ""
        morph = decode_hebrew(morph_raw)
        gloss_es = ""
    else:
        w = cols[1].strip()
        gm = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", w)
        original = (gm.group(1) if gm else w).strip()
        translit = (gm.group(2) if gm else "").strip()
        gloss = cols[2].strip()
        sm = cols[3] if len(cols) > 3 else ""
        raw_strongs = sm.split("=")[0]
        morph_raw = sm.split("=", 1)[1] if "=" in sm else ""
        morph = decode_greek(morph_raw)
        gloss_es = cols[8].strip() if len(cols) > 8 else ""

    sm = STRONGS_RE.search(raw_strongs or "")
    strongs = norm_strongs(sm.group(1)) if sm else ""
    if not original or not strongs:
        return None
    return (vid, pos, original, translit, gloss, gloss_es, strongs, morph, morph_raw)


def ingest(con, path: str):
    files = sorted(glob.glob(os.path.join(path, "**", "TA[HG][ON]T*.txt"), recursive=True))
    # Prefer the current folder over "Older Formats" when both are present.
    files = [f for f in files if "Older Formats" not in f] or files
    if not files:
        raise SystemExit(f"No TAHOT/TAGNT files under {path}")

    total = 0
    for fp in files:
        hebrew = "TAHOT" in os.path.basename(fp)
        rows = []
        with open(fp, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line or line[0] in "#\n\t":
                    continue
                r = parse_line(line, hebrew)
                if r:
                    rows.append(r)
        if rows:
            con.executemany("""
                INSERT OR REPLACE INTO interlinear
                  (vid, position, original, translit, gloss, gloss_es,
                   strongs_id, morph, morph_code)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, rows)
            con.commit()
        total += len(rows)
        print(f"  {os.path.basename(fp)[:48]:<50} {len(rows):>7,} words")

    # verse_words is what the word-study panel joins against; derive it here so
    # there is one source of truth and no second parser to keep in step.
    con.execute("DELETE FROM verse_words")
    con.execute("""
        INSERT OR REPLACE INTO verse_words (vid, position, strongs_id, surface)
        SELECT vid, position, strongs_id, COALESCE(NULLIF(gloss,''), original)
        FROM interlinear
    """)
    con.commit()
    print(f"\n  interlinear: {total:,} words")
    print(f"  verse_words: {con.execute('SELECT COUNT(*) FROM verse_words').fetchone()[0]:,} rebuilt")


def main():
    ap = argparse.ArgumentParser(description="Ingest STEPBible interlinear data")
    ap.add_argument("--db", default="data/apparatus.db")
    ap.add_argument("--stepbible", required=True,
                    help="path to a clone of STEPBible/STEPBible-Data")
    args = ap.parse_args()
    con = connect(args.db)
    ingest(con, args.stepbible)
    con.close()


if __name__ == "__main__":
    main()
