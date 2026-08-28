"""
Retrieval: build the evidence packet for a passage.

This is the heart of the app. The model never answers from its own memory of
scripture — it answers from what this module hands it, and every item carries a
siglum (a short ID like `MHC:1204` or `H430`) that the model must cite and the
validator will later check.

Hybrid retrieval, because neither half is sufficient alone:
  - range overlap  : commentary explicitly written on these verses (precision)
  - vector search  : passages elsewhere that speak to the same idea (recall)
  - FTS keyword    : catches proper nouns and rare terms embeddings blur away
"""
from dataclasses import dataclass, field, asdict

from . import refs, store
from .embed import embed_query


@dataclass
class Evidence:
    """Everything the model is allowed to reason from, plus its sigla."""
    passage: list[dict] = field(default_factory=list)
    translations: list[dict] = field(default_factory=list)
    words: list[dict] = field(default_factory=list)
    cross_refs: list[dict] = field(default_factory=list)
    commentary: list[dict] = field(default_factory=list)

    def sigla(self) -> set[str]:
        """Every citable ID in this packet. The validator's allow-list."""
        s = {c["siglum"] for c in self.commentary}
        s |= {w["siglum"] for w in self.words}
        s |= {x["siglum"] for x in self.cross_refs}
        s |= {v["siglum"] for v in self.passage}
        return s

    def to_dict(self):
        return asdict(self)


def actual_range(con, start: int, end: int, translation: str):
    """
    Resolve a parsed reference to the verses that actually exist.

    A whole-chapter reference parses with a sentinel end of verse 999, meaning
    "however many there are". Anything that measures the span before resolving
    it sees 998 verses and concludes the request is enormous. Clamp first,
    then measure.

    Returns (start, end, count); count is 0 when nothing matches.
    """
    rows = store.rows(con, """
        SELECT MIN(vid) AS lo, MAX(vid) AS hi, COUNT(*) AS n
        FROM verses WHERE translation = ? AND vid BETWEEN ? AND ?
    """, (translation, start, end))
    if not rows or not rows[0]["n"]:
        return start, end, 0
    r = rows[0]
    return int(r["lo"]), int(r["hi"]), int(r["n"])


def _rows(con, sql, args=()):
    """Delegates to the store so the same SQL runs on SQLite and Postgres."""
    return store.rows(con, sql, args)


def passage_text(con, start: int, end: int, translations: list[str]) -> list[dict]:
    out = []
    for t in translations:
        rows = _rows(
            con,
            "SELECT vid, text FROM verses WHERE translation=? AND vid BETWEEN ? AND ? ORDER BY vid",
            (t, start, end),
        )
        for r in rows:
            out.append({
                "siglum": f"{t}:{refs.osis(r['vid'])}",
                "translation": t,
                "ref": refs.label(r["vid"]),
                "vid": r["vid"],
                "text": r["text"].strip(),
            })
    return out


def interlinear(con, start: int, end: int, lang: str = "en") -> list[dict]:
    """
    Word-by-word original text for the reading pane.

    Distinct from original_words() below: that one picks the few rare terms
    worth explaining, this one returns every word in order so the reader can
    follow the actual sentence.
    """
    rows = store.rows(con, """
        SELECT i.vid, i.position, i.original, i.translit, i.gloss, i.gloss_es,
               i.strongs_id, i.morph, s.lang AS script
        FROM interlinear i
        LEFT JOIN strongs s ON s.id = i.strongs_id
        WHERE i.vid BETWEEN ? AND ?
        ORDER BY i.vid, i.position
    """, (start, end))

    out = []
    for r in rows:
        gloss = r["gloss"] or ""
        if lang == "es" and (r["gloss_es"] or "").strip():
            gloss = r["gloss_es"]
        out.append({
            "siglum": r["strongs_id"] or "",
            "vid": r["vid"], "ref": refs.label(r["vid"], lang),
            "position": r["position"],
            "original": r["original"], "translit": r["translit"] or "",
            "gloss": gloss, "strongs": r["strongs_id"] or "",
            "morph": r["morph"] or "",
            "script": r["script"] or ("hebrew" if (r["strongs_id"] or "").startswith("H") else "greek"),
        })
    return out


def original_words(con, start: int, end: int, limit: int = 24) -> list[dict]:
    """
    Hebrew/Greek words in the passage, joined to Strong's.

    Ranked by rarity: a word appearing 12 times in the whole canon is far more
    likely to be worth explaining than one appearing 8,000 times. This is a
    cheap heuristic that consistently surfaces the interesting terms.
    """
    rows = _rows(con, """
        SELECT vw.vid, vw.position, vw.surface, s.id, s.lang, s.lemma,
               s.translit, s.definition, s.derivation, s.kjv_usage,
               i.morph, i.gloss,
               (SELECT COUNT(*) FROM verse_words x WHERE x.strongs_id = s.id) AS corpus_freq
        FROM verse_words vw
        JOIN strongs s ON s.id = vw.strongs_id
        LEFT JOIN interlinear i ON i.vid = vw.vid AND i.position = vw.position
        WHERE vw.vid BETWEEN ? AND ?
        ORDER BY vw.vid, vw.position
    """, (start, end))

    seen, out = set(), []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append({
            "siglum": r["id"],
            "ref": refs.label(r["vid"]),
            "surface": r["surface"],
            "lemma": r["lemma"],
            "translit": r["translit"],
            "lang": r["lang"],
            "definition": (r["definition"] or "").strip(),
            "derivation": (r["derivation"] or "").strip(),
            "kjv_usage": (r["kjv_usage"] or "").strip(),
            "corpus_freq": r["corpus_freq"],
            "morph": r["morph"] or "",
            "in_context": r["gloss"] or "",
        })
    out.sort(key=lambda w: w["corpus_freq"])
    return out[:limit]


def cross_references(con, start: int, end: int, limit: int = 12,
                     translation: str = "BSB") -> list[dict]:
    rows = _rows(con, """
        SELECT to_start, to_end, MAX(votes) AS votes
        FROM cross_refs
        WHERE from_vid BETWEEN ? AND ?
        GROUP BY to_start, to_end
        ORDER BY votes DESC
        LIMIT ?
    """, (start, end, limit))

    out = []
    for r in rows:
        text = _rows(con, """
            SELECT text FROM verses
            WHERE translation = ? AND vid BETWEEN ? AND ?
            ORDER BY vid LIMIT 4
        """, (translation, r["to_start"], r["to_end"]))
        out.append({
            "siglum": refs.osis(r["to_start"]),
            "ref": refs.range_label(r["to_start"], r["to_end"]),
            "votes": r["votes"],
            "text": " ".join(t["text"].strip() for t in text),
        })
    return [x for x in out if x["text"]]


def commentary(con, start: int, end: int, question: str | None,
               per_source: int = 2, semantic_k: int = 6,
               lens: str | None = None) -> list[dict]:
    """
    Direct hits first (commentary written on this passage), then semantic
    neighbours. Capped per source so one verbose commentator can't drown out
    the others -- which matters, because balance across traditions is the
    point of showing multiple commentaries at all.
    """
    direct = _rows(con, """
        SELECT c.id, c.source_id, c.start_vid, c.end_vid, c.text,
               s.title, s.author, s.year, s.tradition, s.license
        FROM chunks c JOIN sources s ON s.id = c.source_id
        WHERE c.start_vid <= ? AND c.end_vid >= ?
        ORDER BY c.source_id, (c.end_vid - c.start_vid) ASC
    """, (end, start))

    # The reader's own tradition gets a slightly larger allowance so its
    # position can be stated in full. Deliberately +1, not a takeover: the
    # other traditions keep their slots, because a packet that only contains
    # one side cannot produce an honest disputed section no matter what the
    # prompt says.
    lens_sources = set()
    if lens:
        lens_sources = {r["source_id"] for r in direct
                        if (r.get("tradition") or "") == lens}

    picked, counts = [], {}
    for r in sorted(direct, key=lambda x: 0 if x["source_id"] in lens_sources else 1):
        cap = per_source + 1 if r["source_id"] in lens_sources else per_source
        if counts.get(r["source_id"], 0) >= cap:
            continue
        counts[r["source_id"]] = counts.get(r["source_id"], 0) + 1
        picked.append(dict(r, retrieval="direct", score=1.0))

    chosen_ids = {r["id"] for r in picked}

    # Only load the embedding model if there is something to search. Lets the
    # app run usefully on scripture + lexicon + cross-references alone, before
    # any commentary has been ingested.
    # Semantic retrieval is one of several signals. If the embedding service
    # is down, rate limited, or misconfigured, the study should still be built
    # from range matching, keyword search and cross-references rather than
    # failing outright.
    semantic_hits = []
    if question and store.has_vectors(con):
        try:
            qvec = embed_query(question)
            semantic_hits = store.vector_search(con, qvec, semantic_k, chosen_ids)
        except Exception as e:
            import sys
            print(f"[embed] query embedding failed ({e}); "
                  f"continuing without semantic search", file=sys.stderr)

    if question:
        for cid, score in semantic_hits:
            if score < 0.32:      # below this, results are noise
                continue
            row = _rows(con, """
                SELECT c.id, c.source_id, c.start_vid, c.end_vid, c.text,
                       s.title, s.author, s.year, s.tradition, s.license
                FROM chunks c JOIN sources s ON s.id = c.source_id
                WHERE c.id = ?
            """, (cid,))
            if row:
                picked.append(dict(row[0], retrieval="semantic", score=score))
                chosen_ids.add(cid)

        # Keyword pass: embeddings smooth over rare proper nouns.
        for r in store.keyword_search(con, question, limit=4):
            if r["id"] not in chosen_ids:
                picked.append(dict(r, retrieval="keyword", score=0.5))
                chosen_ids.add(r["id"])

    out = []
    for r in picked:
        out.append({
            "siglum": f"{r['source_id'].upper()}:{r['id']}",
            "source_id": r["source_id"],
            "source": r["title"],
            "author": r["author"],
            "year": r["year"],
            "tradition": r["tradition"],
            "license": r["license"],
            # Lets validate() know whether a lexical overlap check is meaningful.
            "source_lang": (r["tradition"] or "").startswith("es:") and "es" or "en",
            "on": refs.range_label(r["start_vid"], r["end_vid"]),
            "retrieval": r["retrieval"],
            "score": round(r["score"], 3),
            "text": r["text"].strip(),
        })
    return out


def build_evidence(con, start: int, end: int, question: str | None,
                   translations: list[str], lens: str | None = None) -> Evidence:
    q = question or refs.range_label(start, end)
    return Evidence(
        passage=passage_text(con, start, end, translations),
        translations=[{"id": t} for t in translations],
        words=original_words(con, start, end),
        cross_refs=cross_references(con, start, end,
                                    translation=translations[0]),
        commentary=commentary(con, start, end, q, lens=lens),
    )
