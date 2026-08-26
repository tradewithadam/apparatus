# Apparatus

A Bible study tool that shows its work.

You type in a passage. It retrieves real sources — lexicon entries, cross-references,
commentators from several traditions — and produces a study where every claim is
traceable to something retrieved, and where questions the church has genuinely
argued about for centuries are presented as arguments rather than answers.

---

## Why this exists

Ask a general-purpose AI about Jeremiah 29:11 and you will get a fluent, warm,
confident answer that treats it as a personal promise about your future. It is
addressed to exiles in Babylon about a national restoration seventy years out.
The answer will sound wonderful and it will be wrong.

That failure mode is not fixable with a better prompt. It is fixable with
architecture:

1. **Retrieve first.** The model never answers from its own memory of scripture.
   It answers from an evidence packet assembled by `retrieval.py`.
2. **Cite by siglum.** Every item in the packet carries a short ID — `G1344`,
   `MHC:1204`, `BSB:Rom.5.1`. The model must attach these to every claim.
3. **Verify, don't trust.** `validate.py` checks every citation against the
   packet and **deletes claims whose citations don't resolve**, then reports how
   many it removed. The user sees that number.
4. **Sort settled from disputed.** The output schema forces the model to place
   contested doctrine in a `disputed` array where each entry needs two or more
   positions with *named* traditions holding them. It is structurally unable to
   quietly pick a side.

Point 3 is the one nobody else does. Everything else is a prompt; that one is a test.

---

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # add your ANTHROPIC_API_KEY

# Scripture, cross-references, lexicon (~10 min, ~250 MB)
python -m app.ingest.corpus --db data/apparatus.db --all

python run.py               # http://localhost:5000
```

It's useful immediately with just that. Commentary makes it much better:

```bash
python scripts/fetch_commentaries.py --out corpora     # ~5 min, 66k entries
python -m app.ingest.commentary --dir corpora --embed  # ~20 min (embedding)
```

That pulls six public-domain commentaries from CrossWire's SWORD library
across five traditions — Matthew Henry, JFB, Barnes, Clarke, Wesley, and the
Catena Aurea. The `--embed` step is the slow part; drop it to get keyword and
range retrieval working immediately and run it later.

Optional word-study panel:

```bash
git clone --depth 1 https://github.com/STEPBible/STEPBible-Data.git
python -m app.ingest.corpus --stepbible STEPBible-Data
```

---

## Verified working

Everything below was run against the live sources, not assumed:

| | |
|---|---|
| Berean Standard Bible | 31,086 verses |
| King James Version | 31,102 verses |
| Cross-references (openbible.info) | 343,558 links |
| Hebrew Strong's | 8,674 entries |
| Greek Strong's | 5,523 entries |
| Reference parser | handles `1 Cor 13`, `first john 4:8`, `II Peter 1:3`, `Ps 23` |
| Validator | caught 4/4 planted hallucinations in the test fixture |

---

## Licensing — the part that bites people

**Bible translations are copyrighted and publishers enforce it.** This is the
most common way a well-meaning faith app gets a letter from a lawyer.

**Ship these freely:**

- **BSB** — Berean Standard Bible, dedicated to the public domain in 2023.
  Modern readable English with no licence to negotiate. This is the default here
  and it is the best deal in the entire space.
- **WEB** — World English Bible, public domain by explicit dedication
- **KJV** — public domain (still Crown copyright inside the UK)
- **ASV** (1901), **YLT** — public domain

**These need permission:**

- **ESV** — free non-commercial API tier at `api.esv.org`, ~5k calls/day
- **NIV** (Biblica), **NASB** (Lockman), **CSB** (Holman), **NLT** (Tyndale)

Do not scrape a translation off a website into your database. `api.scripture.api.bible`
fronts many translations on a free developer tier with per-translation terms you
still have to honour.

Supporting data, all cleanly licensed:

| Source | Licence |
|---|---|
| openbible.info cross-references | CC BY |
| Strong's Concordance (openscriptures) | public domain |
| STEPBible TAHOT/TAGNT | CC BY 4.0 |
| Matthew Henry, JFB, Barnes, Gill, Clarke, Wesley, Catena Aurea | public domain |

`commentary.py` **refuses to ingest** a corpus whose `_source` record has no
`license` field. That's deliberate — make it annoying to be careless.

---

## Build your commentary corpus with breadth on purpose

The `disputed` feature only works if the evidence packet actually contains
disagreement. Ingest one commentator and the model has nothing to disagree with,
so it will either invent the other side or present one tradition as *the*
Christian position. Both are the failure you built this to avoid.

| Commentary | Character | Tradition |
|---|---|---|
| Matthew Henry (1710) | devotional, warm | puritan |
| Jamieson-Fausset-Brown | concise, critical | reformed |
| Albert Barnes (1830s) | detailed, exegetical | presbyterian |
| John Gill (1748) | heavy, verbose | particular baptist |
| Adam Clarke (1831) | philological, independent | methodist/wesleyan |
| Wesley's Notes | brief, practical | methodist |
| Keil & Delitzsch (OT) | technical Hebrew | lutheran |
| Catena Aurea (Aquinas) | patristic chains | catholic/patristic |

All public domain, all available in bulk from CCEL and StudyLight. Clarke and
Wesley pull the centre of gravity away from an all-Reformed panel; Catena Aurea
brings the church fathers in.

The app warns you in the status bar if fewer than three traditions are loaded.

---

## Layout

```
app/
  refs.py          reference parsing; integer verse keys for range queries
  db.py            SQLite schema
  retrieval.py     builds the evidence packet (range + vector + keyword)
  prompts.py       system prompt and the output schema that does the real work
  llm.py           forced tool-use call, so JSON is guaranteed well-formed
  validate.py      the citation validator — strips ungrounded claims
  embed.py         local sentence-transformers by default, Voyage optional
  ingest/
    corpus.py      scripture, cross-references, Strong's
    commentary.py  chunking + embedding
  templates/
    index.html     reading pane + apparatus
```

SQLite throughout. At this scale (~62k verses, ~340k cross-refs, ~150k chunks)
it does not break a sweat, and it backs up with `cp`.

Vector search is brute-force numpy — roughly 40ms over 150k chunks. An ANN index
buys you milliseconds nobody will notice and a dependency that will break on
upgrade. Revisit past ~1M chunks.

---

## Things worth knowing before you build on this

**Have a pastor read twenty outputs before anyone else sees it.** Pick the hard
passages on purpose: Romans 9, James 2, 1 Corinthians 11, Genesis 1, Revelation 20.
You are looking for one specific failure — a `settled` claim that should have been
`disputed`. That is the bug that matters, and you cannot catch it yourself on
passages where you already hold a view.

**The `certainty` grades will drift toward `explicit`.** Models are agreeable and
`explicit` sounds more helpful. Spot-check them. If you find drift, tighten the
schema description before you touch the system prompt — the schema is what the
model actually obeys.

**Don't add a "what does this mean for my life today" feature.** It is the most
requested thing and the most dangerous. Application requires knowing the person,
and this thing knows a passage and some commentaries. Leave that to the pastors
you're pointing people toward.

**The grounding score is a feature, not a metric to optimise.** If it reads 100%
every time, your validator has stopped catching anything and you should go plant
a fabrication in a test fixture to check it still fires.

---

*A study aid, not a teacher.*
