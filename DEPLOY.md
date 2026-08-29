# Putting Ad Fontes online

An afternoon if it goes smoothly. The order below matters — step 0 is the one
that protects your card.

---

## 0. Before anything else

**Set a cost limit.** console.anthropic.com → **Billing → Cost limits**. Not the
Rate limits page — those are throughput caps set by Anthropic and are a
different thing entirely. Pick a monthly ceiling; $20 covers heavy personal use.

This is the backstop for everything below. The app has its own guards, but a cap
in the console is the one nothing in your code can get past.

---

## Why not Supabase after all

The old version of this file said Postgres. That was written before you had a
corpus. Your database now:

| | |
|---|---|
| Commentary chunks + embeddings | ~260 MB |
| Interlinear (425k words) | ~70 MB |
| Cross-references | ~40 MB |
| Translations, verse vectors, Strong's | ~75 MB |
| **Total** | **~450 MB** |

Supabase's free tier is 500 MB, so you'd start at $25/month with no users and no
headroom. A Render persistent disk is about **$1.25/month for 5 GB** and needs no
migration — the SQLite file you already have just goes on it.

**~$8/month**, versus ~$32.

---

## 1. Package the database

```bash
python scripts/prepare_deploy.py
```

VACUUMs, gzips, prints the size. SQLite compresses well — expect roughly a third
of the original.

Upload the result as a **GitHub Release asset** (repo → Releases → Draft new
release → attach file). Free, fine up to 2 GB, stable download URL.

Do *not* commit it to git. A 400 MB binary in version control makes every clone
painful forever.

---

## 2. Push the code

```bash
git add . && git commit -m "Ready to deploy" && git push
```

Check nothing sensitive is going up:

```bash
git status --ignored | grep -E "\.env|\.db|\.jsonl"
```

---

## 3. Render

Dashboard → **New → Blueprint** → your repo. It reads `render.yaml` and creates
the service with a 5 GB disk mounted at `/var/data`.

Set these in the dashboard (marked `sync: false`, so Render prompts rather than
reading them from git):

```
ANTHROPIC_API_KEY   sk-ant-...
VOYAGE_API_KEY      pa-...      (embeddings — keeps PyTorch out of the build)
DB_URL              https://github.com/YOU/apparatus/releases/download/v1/apparatus-db.sqlite.gz
ACCESS_CODE         something   (optional — see step 5)
```

First boot downloads the database onto the disk. Watch the logs for
`[bootstrap] database ready`. **This happens once, ever** — the disk survives
deploys, so pushing code later doesn't re-download it.

If the download fails the app still starts, and says in plain language what
isn't loaded rather than showing a stack trace.

Check `/api/status`. You want your real numbers: ~93k verses, ~120k commentary
chunks, 9 traditions.

---

## 4. The spending guards

Three layers, set in `render.yaml`, adjustable in the dashboard:

| | Default | What it stops |
|---|---|---|
| `RATE_PER_HOUR` | 20 | One person hammering it |
| `RATE_PER_DAY` | 60 | One person grinding through the Bible overnight |
| `GLOBAL_DAILY_CAP` | 500 | **Everyone together emptying your account** |

The global cap is the one that matters. Per-IP limits are defeated by anyone who
cares — a few proxies and each gets a fresh allowance. A hard ceiling on total
model calls per day can't be routed around, and it turns your worst case into a
number you chose.

At roughly 2¢ a study, 500/day is about $10/day worst case. Set it to what
you're willing to lose in a day, not to what you expect to use.

Only the three model-calling endpoints are limited. Reading scripture, browsing
books, notes, and history stay free and unmetered.

Check usage any time at `/api/usage`.

---

## 5. Share it privately first

Set `ACCESS_CODE` to anything and the app asks for it before running a study.
Reading scripture still works without it.

Give the code to your dad, Diana's dad, and eight other people. Watch for two
weeks. Then decide whether to remove it.

Costs nothing, and means the first strangers arrive after someone who knows
theology has looked at the output.

---

## 6. Test it properly

From your phone, **off wifi**:

- Passage study end to end
- Switch to Español, run `Juan 3:16`
- A topic search
- Build a sermon, hit Print
- Open history, reopen something
- Write a note

Then deliberately hit the limit — run 21 studies — and check you get the plain
message rather than a raw error.

---

## Costs

| | |
|---|---|
| Render starter | $7/mo |
| 5 GB disk | ~$1.25/mo |
| Anthropic | ~2¢ per study, capped by your guards |
| Voyage embeddings | negligible after ingest |

The cache means a repeated passage is free, which matters more than it sounds —
people study the same fifty passages.

---

## When something breaks

**`[bootstrap] failed` in the logs** — `DB_URL` is wrong or the release asset is
private. The app still runs; fix and redeploy.

**`/api/status` shows 0 for everything** — the database never landed. Check
`DB_PATH` is `/var/data/apparatus.db` and the disk is mounted.

**Everyone rate limited at once** — `X-Forwarded-For` isn't reaching the app, so
every visitor looks like one IP. Check there's no extra proxy in front.

**First request takes a minute** — free tier spin-down. That's the $7.

**Build fails, out of memory** — something pulled in `sentence-transformers`.
Production uses `requirements.txt` and `EMBED_BACKEND=voyage`.

---

## Before you take the access code off

Get a pastor to read twenty outputs on hard passages — Romans 9, James 2,
1 Corinthians 11, Genesis 1, Revelation 20.

You're looking for one specific failure: a claim in **Broadly agreed** that
belongs in **Faithfully disputed**. You can't catch that yourself on a passage
where you already hold the view, and neither can the tool.

There's a "Something wrong?" button on every study now, and reports land at
`/api/feedback`. That's the list to review with them.
