# Start here

Getting Apparatus running on your machine. About 20 minutes, most of it waiting.

---

## Step 1 — Unzip it

Download `apparatus.zip` and unzip it somewhere you'll find it again.
`C:\Users\You\Projects\apparatus` or `~/Projects/apparatus` is fine.

You should end up with a folder called `apparatus` containing `run.py`, a
`README.md`, and an `app` folder. If you unzipped into a folder that contains
*another* folder called `apparatus`, go one level in — you want the one with
`run.py` sitting directly inside it.

---

## Step 2 — Open it in VS Code

VS Code → **File → Open Folder** → pick the `apparatus` folder.

Not "Open File." Open **Folder**. VS Code needs the whole project so the
terminal starts in the right place, which saves you a lot of `cd` confusion.

Then open the built-in terminal: **Ctrl+`** (that's the backtick key, above Tab,
left of the 1). Or **View → Terminal**.

Everything below gets typed in that terminal.

---

## Step 3 — Check you have Python

```
python --version
```

You want 3.10 or higher. If it says 3.12, great.

If you get "command not found" or a Microsoft Store popup, install Python from
[python.org](https://python.org) — and **check the "Add Python to PATH" box** on
the first screen of the installer, which is easy to miss and annoying to fix
later. Then close VS Code, reopen it, and try again.

On Mac, `python3 --version`. If you're on Mac, use `python3` and `pip3`
everywhere below.

---

## Step 4 — Make a virtual environment

This keeps this project's libraries separate from everything else on your
machine. One line:

```
python -m venv venv
```

Then turn it on:

**Windows:**
```
venv\Scripts\activate
```

**Mac / Linux:**
```
source venv/bin/activate
```

Your terminal prompt should now start with `(venv)`. That's how you know it
worked. **You need to do this every time you open a new terminal for this
project** — if something later says "module not found," this is almost always
why.

> **Windows note:** if activate fails with something about "running scripts is
> disabled," run this once, then try again:
> ```
> Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
> ```

VS Code will probably pop a toast asking if you want to use this environment —
say yes.

---

## Step 5 — Install the libraries

```
pip install -r requirements-local.txt
```

**This one takes a while** — 5 to 10 minutes and around 2 GB. Most of that is
PyTorch, which powers the offline text-search model. Go get coffee.

You'll see a lot of scrolling. That's normal. If it ends without a red `ERROR`,
you're good.

---

## Step 6 — Add your API key

You need a key from [console.anthropic.com](https://console.anthropic.com) →
**API Keys** → **Create Key**. Copy it — it's only shown once. Put $5 on the
account under Billing; that's hundreds of studies.

Now make your settings file. In the VS Code file list on the left, find
`.env.example`, right-click → **Copy**, then right-click in the empty space →
**Paste**. Rename the copy to exactly `.env` (no `.example`, and yes it starts
with a dot).

Open `.env` and put your key on the first line:

```
ANTHROPIC_API_KEY=sk-ant-your-actual-key-here
```

Save it. **Ctrl+S.**

Leave everything else in that file alone. The `DATABASE_URL` line stays
commented out — that's what keeps you running locally instead of trying to reach
a server that doesn't exist yet.

`.env` is already in `.gitignore`, so your key won't end up on GitHub.

---

## Step 7 — Download the scripture data

```
python -m app.ingest.corpus --all
```

**Another 10 minutes.** This pulls down two Bible translations, 343,000
cross-references, and the full Hebrew and Greek lexicons, and builds your
database.

You'll see it count up:

```
  BSB: 31,086 verses
  KJV: 31,102 verses
  cross-references: 343,558 links
  Hebrew Strong's: 8,674 entries
  Greek Strong's: 5,523 entries
Done.
```

You only do this once. It's a real download from real archives, so it needs
decent internet.

---

## Step 8 — Run it

```
python run.py
```

You'll see something like:

```
 * Running on http://127.0.0.1:5000
```

Ctrl+click that link, or just open a browser to **http://localhost:5000**.

Try one of the example buttons. Jeremiah 29:11 is the one worth trying first —
it's the passage most commonly misused, and watching the tool handle it honestly
is the whole point.

To stop the server: **Ctrl+C** in the terminal.

---

## Coming back to it later

Every time you sit down to work on this:

```
cd path\to\apparatus
venv\Scripts\activate          # Mac: source venv/bin/activate
python run.py
```

Three lines. The install and the data download are one-time.

---

## When something goes wrong

**`python: command not found`**
Python isn't installed or isn't on PATH. Reinstall from python.org with the
"Add to PATH" box checked.

**`No module named flask`**
Your virtual environment isn't active. Look for `(venv)` at the start of your
prompt. Run the activate command from Step 4.

**`ANTHROPIC_API_KEY is not set`**
The `.env` file is named wrong (check it's not `.env.txt` — Windows hides
extensions by default), or it's not in the `apparatus` folder next to `run.py`.

**`No text found. Has the corpus been ingested?`**
Step 7 didn't finish. Run it again — it's safe to re-run.

**Port 5000 already in use**
Something else is using it. On Mac it's often AirPlay Receiver. Either turn that
off in System Settings, or run `python run.py` after setting a different port in
`.env`: add a line `PORT=5050`.

**Everything works but the study has no "Faithfully disputed" section**
That's expected right now. That section needs commentary loaded, which is the
next thing to build. Scripture, cross-references, and word study all work
without it.

---

## What's next, when you're ready

The tool is useful now but it's running on scripture and lexicon alone. The
"Faithfully disputed" feature — the thing that makes it trustworthy on hard
passages — needs public-domain commentaries from several traditions loaded in.

That's the next build. Nothing else is required first.

Ignore `DEPLOY.md`, `render.yaml`, `app/store.py`, and the `scripts` folder for
now. They're for putting this online later. The app doesn't touch them.
