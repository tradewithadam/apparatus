"""
Topical search: "what does scripture say about X".

This is a different problem from passage study and it carries a specific danger.
A topical Bible search that simply returns the verses most similar to your query
is a proof-texting machine. Ask it for "verses supporting reincarnation" and it
will hand you the six that sound closest, and a model will happily build a case
from them. That is how people get hurt by tools like this.

Two design decisions push against that:

1. The query sent to retrieval is neutralised. "verses supporting X" and
   "verses refuting X" both retrieve the same evidence set -- the passages that
   bear on X. What differs is what the model is asked to do with them, and it
   is asked to present the range, never to build a case.

2. Retrieval deliberately surfaces the passages people *misuse* on a topic, not
   only the ones that support it, via the cross-reference hub pass below.

WHY SEMANTIC SEARCH ALONE FAILS HERE (measured, not assumed)

Embedding every verse and searching by cosine similarity seems like the obvious
design. It does not work for topical questions, and the failure is structural
rather than a tuning problem.

Asked for "reincarnation", pure semantic search over single verses returned
Job 33:25, Psalm 85:6, Psalm 104:30 -- verses containing "soul", "revive",
"renew". It missed Hebrews 9:27, John 9:2, and Matthew 11:14, which are the
three passages the entire discussion actually turns on. Hebrews 9:27 says
"appointed to die once, and after that the judgment" and contains no word
resembling the query. A verse is about a topic because of what it *means*, and
single-verse embeddings match on vocabulary.

So candidate references are proposed by the model first, then EVERY one is
verified against the database and its real text retrieved. The model's scriptural
recall is used only to find candidates -- never for what they say. A reference it
invents simply fails lookup and disappears. Semantic and keyword search still run
underneath, catching what the model misses.

THE CROSS-REFERENCE HUB TRICK

Semantic search finds verses that *sound* like the query. That misses the verse
everyone actually argues about, because theological weight and lexical
similarity are different things.

But we have 343,000 human-curated cross-references. If twelve of the top-forty
semantic hits all point at Hebrews 9:27, that verse is central to the topic
regardless of how it scores on cosine similarity. Counting inbound references
from the semantic result set finds the passages the tradition treats as load-
bearing. It is cheap, it uses data already sitting in the database, and it
consistently surfaces the verses that matter.
"""
import re
from collections import Counter

from . import refs, store
from .embed import embed_query

# Words that wreck keyword search when a topic phrase leaves them behind.
_KW_STOP = {
    "like", "what", "does", "about", "bible", "verse", "verses", "scripture",
    "says", "said", "tell", "know", "there", "their", "them", "this", "that",
    "with", "from", "have", "will", "when", "where", "which", "would", "into",
    "sobre", "acerca", "dice", "biblia", "como", "para", "esta", "este",
}

# Framing words stripped before retrieval, so that asking for support and asking
# for refutation return the same evidence.
_STANCE = re.compile(
    r"\b(vers(?:e|es|[ií]culos?)|passages?|pasajes?|scriptures?|escrituras?|texts?|textos?)?\s*"
    r"(that\s+|que\s+)?"
    r"(support(?:ing)?|prove|proving|refut(?:e|ing|an|ando)|contradict(?:ing|en)?|"
    r"disprove|disproving|against|for|debunk(?:ing)?|"
    r"apoyan?|apoyando|niegan|demuestran|prueban|"
    r"en contra de|a favor de)\b",
    re.I,
)
_FILLER = re.compile(r"\b(what does the bible say about|what does scripture say about|"
                     r"bible verses about|what is|"
                     r"qu[eé] dice la biblia (?:sobre|acerca de)|"
                     r"vers[ií]culos (?:sobre|acerca de)|qu[eé] es)\b", re.I)
# Leftover connective words once the stance verb is gone.
_ORPHANS = re.compile(r"^\s*(que|la|el|los|las|de|del|the|a|an|about|on)\s+", re.I)


def neutralise(topic: str) -> str:
    """'verses contradicting reincarnation' -> 'reincarnation'."""
    t = topic.replace("¿", " ").replace("¡", " ")
    t = _FILLER.sub(" ", t)
    t = _STANCE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" ?.,;:")
    prev = None
    while prev != t:                 # strip stacked orphans: "que la reencarnación"
        prev = t
        t = _ORPHANS.sub("", t).strip()
    return t or topic.strip()


def _verse_rows(con, vids, translation):
    if not vids:
        return {}
    marks = ",".join("?" * len(vids))
    rows = store.rows(con, f"""
        SELECT vid, text FROM verses
        WHERE translation = ? AND vid IN ({marks})
    """, (translation, *vids))
    return {r["vid"]: r["text"] for r in rows}


def semantic_verses(con, query: str, translation: str, k: int = 40):
    """
    Nearest verses by embedding. One of four retrieval signals — returns
    nothing rather than raising if embeddings are unavailable, so a topic
    search still runs on the other three.
    """
    if not store.has_vectors(con):
        return []
    try:
        qvec = embed_query(query)
    except Exception as e:
        import sys
        print(f"[embed] verse embedding failed ({e}); skipping semantic pass",
              file=sys.stderr)
        return []

    if store.IS_PG:
        lit = "[" + ",".join(f"{float(x):.6f}" for x in qvec) + "]"
        with con.cursor() as cur:
            cur.execute("""
                SELECT vid, 1 - (embedding <=> %s::vector) AS score
                FROM verse_vecs WHERE translation = %s
                ORDER BY embedding <=> %s::vector LIMIT %s
            """, (lit, translation, lit, k))
            return [(r["vid"], float(r["score"])) for r in cur.fetchall()]

    import numpy as np
    ids, mat = [], []
    for row in con.execute(
            "SELECT vid, embedding FROM verse_vecs WHERE translation = ?", (translation,)):
        ids.append(row["vid"])
        mat.append(np.frombuffer(row["embedding"], dtype=np.float32))
    if not ids:
        return []
    mat = np.vstack(mat)
    mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    q = np.asarray(qvec, dtype=np.float32)
    q /= (np.linalg.norm(q) + 1e-9)
    scores = mat @ q
    top = np.argsort(-scores)[:k]
    return [(ids[i], float(scores[i])) for i in top]


def keyword_verses(con, query: str, translation: str, limit: int = 20):
    """
    Verses containing ALL the content words of the phrase, not any of them.

    OR was the reason "do animals go to heaven" returned a verse about a
    donkey: the model proposes search phrases like "beasts of the field", each
    word gets its own OR clause, and every verse mentioning any animal at all
    matches. AND makes a phrase behave like a phrase.
    """
    terms = [t for t in re.findall(r"[A-Za-zÀ-ÿ]{4,}", query)
             if t.lower() not in _KW_STOP][:6]
    if not terms:
        return []
    joiner = " AND "
    if store.IS_PG:
        return [(r["vid"], 0.5) for r in store.rows(con, """
            SELECT vid FROM verses
            WHERE translation = ?
              AND to_tsvector('english', text) @@ websearch_to_tsquery('english', ?)
            LIMIT ?
        """, (translation, joiner.join(terms), limit))]
    try:
        return [(r["vid"], 0.5) for r in store.rows(con, """
            SELECT vid FROM verses_fts
            WHERE verses_fts MATCH ? AND translation = ? LIMIT ?
        """, (joiner.join(terms), translation, limit))]
    except Exception:
        return []


def crossref_hubs(con, seed_vids: list[int], limit: int = 12, min_inbound: int = 2):
    """
    Verses that the seed set collectively points at. See the module docstring --
    this is what surfaces the load-bearing passage on a topic when semantic
    similarity alone would miss it.
    """
    if not seed_vids:
        return []
    marks = ",".join("?" * len(seed_vids))
    rows = store.rows(con, f"""
        SELECT to_start, votes FROM cross_refs
        WHERE from_vid IN ({marks}) AND votes > 0
    """, tuple(seed_vids))

    inbound = Counter()
    weight = {}
    seeds = set(seed_vids)
    for r in rows:
        t = r["to_start"]
        if t in seeds:
            continue
        inbound[t] += 1
        weight[t] = max(weight.get(t, 0), r["votes"])

    hubs = [(v, n) for v, n in inbound.items() if n >= min_inbound]
    hubs.sort(key=lambda x: (-x[1], -weight.get(x[0], 0)))
    return hubs[:limit]


CANDIDATE_TOOL = {
    "name": "propose_passages",
    "description": "List the scripture references that bear on a topic.",
    "input_schema": {
        "type": "object",
        "required": ["search_terms", "candidates"],
        "properties": {
            "search_terms": {
                "type": "array",
                "description": "6-12 words or short phrases that would appear in "
                               "the biblical text itself on this subject. Use the "
                               "vocabulary of scripture, not of theology.",
                "items": {"type": "string"},
            },
            "candidates": {
                "type": "array",
                "description": "20-40 references. Include passages cited on every "
                               "side, and especially ones commonly quoted about this "
                               "topic that may not actually support the popular use. "
                               "Format: 'Hebrews 9:27' or 'John 9:1-3'.",
                "items": {"type": "string"},
            },
        },
    },
}

CANDIDATE_SYSTEM = """You locate scripture references for a topical Bible search. You do not interpret, explain, or take a position — a later stage does that from the actual retrieved text.

List the references that bear on the topic, from every side of it. If a passage is famously quoted on this subject, include it even when you think the popular use misreads it — the point of retrieving it is so the next stage can examine it.

Never state what a passage says. Only where it is. Wrong references are dropped harmlessly at lookup, so err toward including a reference you are unsure of."""


def propose_candidates(topic: str, model: str = "claude-haiku-4-5-20251001"):
    """
    Ask the model which passages bear on the topic.

    Nothing it returns is trusted as content. Every reference is parsed and
    looked up; the text comes from the database. An invented reference either
    fails to parse or returns no rows, and vanishes. A real reference the model
    misremembers the meaning of is harmless, because the meaning is never taken
    from the model here.

    Haiku is used deliberately: this is a recall task, it is fast, and it keeps
    the extra call to a rounding error on the cost of a study.
    """
    from .llm import client, ModelError
    try:
        resp = client().messages.create(
            model=model, max_tokens=1600,
            system=[{"type": "text", "text": CANDIDATE_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            tools=[CANDIDATE_TOOL],
            tool_choice={"type": "tool", "name": "propose_passages"},
            messages=[{"role": "user", "content": f"Topic: {topic}"}],
        )
    except Exception:
        return [], []          # degrade to semantic-only rather than failing
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            data = block.input
            return data.get("search_terms", []), data.get("candidates", [])
    return [], []


def resolve_candidates(con, candidates: list[str], translation: str,
                       max_verses_per_ref: int = 6):
    """Parse and verify proposed references. Unparseable or absent ones vanish."""
    out, dropped = [], []
    for c in candidates[:45]:
        try:
            start, end = refs.parse(c)
        except refs.RefError:
            dropped.append(c)
            continue
        if end - start > max_verses_per_ref:
            end = start + max_verses_per_ref
        rows = store.rows(con, """
            SELECT vid FROM verses WHERE translation = ? AND vid BETWEEN ? AND ?
        """, (translation, start, end))
        if not rows:
            dropped.append(c)
            continue
        out.extend(r["vid"] for r in rows)
    return out, dropped


def build_topic_evidence(con, topic: str, translation: str,
                         lang: str = "en", max_verses: int = 28):
    """
    Assemble the evidence packet for a topical question.

    Returns the same shape as passage evidence -- passage / words / cross_refs /
    commentary, each item carrying a siglum -- so the existing validator works
    on topical studies without modification.
    """
    query = neutralise(topic)

    # 1. Model proposes candidates; every one is verified against the database.
    terms, candidates = propose_candidates(query)
    proposed, dropped = resolve_candidates(con, candidates, translation)

    # 2. Semantic + keyword, using the model's scripture-vocabulary terms as
    #    extra queries -- they match the text far better than the topic word does.
    # Semantic hits are capped and floored. Left unbounded they flood the packet
    # with verses that merely share vocabulary -- searching "hell" pulls every
    # mention of darkness in the Psalms. The floor was set by inspecting real
    # results: below it, hits were reliably noise.
    # A verse earns its place by being found more than one way. One weak
    # keyword hit is how a passing mention of a donkey ends up in a study
    # about the afterlife — it contains an animal word and nothing else.
    # Requiring corroboration costs a little recall and buys a lot of
    # precision, which is the right trade for a tool people trust.
    SEM_FLOOR, SEM_CAP, MIN_SIGNALS = 0.52, 8, 2
    scored, sem_pool, signals = {}, {}, {}
    queries = [query] + [t for t in terms[:8] if t]
    for q in queries:
        for vid, sc in semantic_verses(con, q, translation, k=14):
            if sc >= SEM_FLOOR:
                sem_pool[vid] = max(sem_pool.get(vid, 0), sc)
                signals[vid] = signals.get(vid, 0) + 1
    for q in queries[:5]:
        for vid, sc in keyword_verses(con, q, translation, limit=10):
            sem_pool[vid] = max(sem_pool.get(vid, 0), sc)
            signals[vid] = signals.get(vid, 0) + 1

    # Anything the model named is already corroborated by definition.
    sem_pool = {v: sc for v, sc in sem_pool.items()
                if signals.get(v, 0) >= MIN_SIGNALS or v in set(proposed)}
    # Very short verses ("Then the second", genealogy fragments) score
    # erratically against any query -- too little text for the vector to mean
    # much. Drop them from semantic results; a named or hub verse still gets in.
    if sem_pool:
        lens = _verse_rows(con, list(sem_pool), translation)
        sem_pool = {v: sc for v, sc in sem_pool.items()
                    if len(lens.get(v, "")) >= 45}
    for vid, sc in sorted(sem_pool.items(), key=lambda x: -x[1])[:SEM_CAP]:
        scored[vid] = sc

    # Verified proposals rank above search hits — they were chosen for meaning.
    for vid in proposed:
        scored[vid] = max(scored.get(vid, 0), 0.9)

    seeds = [v for v, _ in sorted(scored.items(), key=lambda x: -x[1])[:30]]
    hubs = crossref_hubs(con, seeds)

    # Hubs get a floor score so they survive the cut even when they read
    # nothing like the query -- which is exactly the case they exist for.
    for vid, inbound in hubs:
        scored[vid] = max(scored.get(vid, 0), 0.55 + 0.02 * inbound)

    chosen = [v for v, _ in sorted(scored.items(), key=lambda x: -x[1])[:max_verses]]
    chosen.sort()
    texts = _verse_rows(con, chosen, translation)
    hub_set = {v for v, _ in hubs}
    proposed_set = set(proposed)

    passage = []
    for vid in chosen:
        if vid not in texts:
            continue
        passage.append({
            "siglum": f"{translation}:{refs.osis(vid)}",
            "translation": translation,
            "ref": refs.label(vid, lang),
            "vid": vid,
            "text": texts[vid].strip(),
            "why_retrieved": ("named as bearing on the topic" if vid in proposed_set
                              else "cross-reference hub" if vid in hub_set
                              else "semantic match"),
            "score": round(scored[vid], 3),
        })

    # Original-language words across the retrieved verses, rarest first.
    words = []
    if chosen:
        marks = ",".join("?" * len(chosen))
        rows = store.rows(con, f"""
            SELECT vw.vid, vw.surface, s.id, s.lang, s.lemma, s.translit,
                   s.definition, s.derivation, s.kjv_usage,
                   (SELECT COUNT(*) FROM verse_words x WHERE x.strongs_id = s.id) AS corpus_freq
            FROM verse_words vw JOIN strongs s ON s.id = vw.strongs_id
            WHERE vw.vid IN ({marks})
        """, tuple(chosen))
        seen = set()
        for r in rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            words.append({
                "siglum": r["id"], "ref": refs.label(r["vid"], lang),
                "surface": r["surface"], "lemma": r["lemma"],
                "translit": r["translit"], "lang": r["lang"],
                "definition": (r["definition"] or "").strip(),
                "derivation": (r["derivation"] or "").strip(),
                "kjv_usage": (r["kjv_usage"] or "").strip(),
                "corpus_freq": r["corpus_freq"],
            })
        words.sort(key=lambda w: w["corpus_freq"])
        words = words[:20]

    # Commentary bearing on any retrieved verse.
    commentary = []
    if chosen:
        from .retrieval import commentary as verse_commentary
        seen_ids = set()
        for vid in chosen[:8]:
            for c in verse_commentary(con, vid, vid, None, per_source=1, semantic_k=0):
                if c["siglum"] not in seen_ids:
                    seen_ids.add(c["siglum"])
                    commentary.append(c)
        commentary = commentary[:14]

    return {
        "topic": topic,
        "neutralised_query": query,
        "search_terms": terms,
        "unresolved_candidates": dropped,
        "passage": passage,
        "words": words,
        "cross_refs": [],
        "commentary": commentary,
    }
