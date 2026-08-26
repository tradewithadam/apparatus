import hashlib
import json
import os
import time

from flask import Flask, jsonify, render_template, request

from . import refs, retrieval, validate, llm, store, topics, limits
from .db import get_db, close_db, init_db


def _year_range(sources):
    """
    Earliest and latest year in the corpus.

    Was comparing year strings and seeding with "9999"/"0000" sentinels, so a
    source with no year left the sentinels in place and the footer advertised
    "written 9999-0000". Parse to integers and ignore anything unparseable.
    """
    years = []
    for s in sources:
        y = (s.get("year") or "").strip()
        if y.isdigit() and 100 <= int(y) <= 2100:
            years.append(int(y))
    if not years:
        return [None, None]
    return [str(min(years)), str(max(years))]


def _embed_warning(con):
    try:
        from . import embed
        return embed.check(con)
    except Exception:
        return None


def _bootstrap_db(path):
    """
    Fetch the prepared database on first boot if the disk is empty.

    Render's persistent disk starts empty and survives deploys, so this runs
    once ever, not once per deploy. Point DB_URL at a gzipped SQLite file — a
    GitHub Release asset works and is free. Without this the alternative is
    committing a 400 MB binary to git or re-running a half-hour ingest on a
    512 MB instance, and neither is a good idea.
    """
    import gzip
    import shutil
    import urllib.request

    url = os.environ.get("DB_URL", "").strip()
    if not url:
        return

    # Re-download when DB_URL changes. Without this, pointing at a rebuilt
    # database does nothing — the disk already has a file, so the fetch is
    # skipped and the old data stays. Changing the URL is the only signal that
    # you meant to replace it.
    marker = path + ".source"
    if os.path.exists(path):
        try:
            with open(marker) as f:
                if f.read().strip() == url:
                    return
        except OSError:
            return          # no marker: an existing database predates this, leave it
        print("[bootstrap] DB_URL changed; replacing the database", flush=True)
        os.remove(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".download"
    print(f"[bootstrap] fetching database from {url}", flush=True)
    try:
        with urllib.request.urlopen(url, timeout=900) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        if url.endswith(".gz"):
            with gzip.open(tmp, "rb") as src, open(path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.remove(tmp)
        else:
            os.replace(tmp, path)
        with open(marker, "w") as f:
            f.write(url)
        size = os.path.getsize(path) / 1e6
        print(f"[bootstrap] database ready ({size:.0f} MB)", flush=True)
    except Exception as e:
        # Boot anyway — an empty database gives clear in-app messages about
        # what is not loaded, which beats a container that will not start.
        print(f"[bootstrap] failed: {e}", flush=True)
        for leftover in (tmp,):
            if os.path.exists(leftover):
                os.remove(leftover)


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        DB_PATH=os.environ.get("DB_PATH", "data/apparatus.db"),
        MODEL=os.environ.get("MODEL", "claude-sonnet-4-6"),
        TRANSLATIONS=os.environ.get("TRANSLATIONS", "BSB,KJV").split(","),
        # Reading text per UI language. Spanish readers get Reina-Valera 1909.
        TRANSLATIONS_ES=os.environ.get("TRANSLATIONS_ES", "SpaRV").split(","),
        CACHE_STUDIES=os.environ.get("CACHE_STUDIES", "1") == "1",
    )
    if config:
        app.config.update(config)

    _bootstrap_db(app.config["DB_PATH"])

    # Only create the schema when serving from a local SQLite file. In
    # production the schema already exists in Supabase, put there by the
    # migration script, and the app should never issue DDL at boot.
    if not store.IS_PG:
        init_db(app.config["DB_PATH"])
    app.teardown_appcontext(close_db)

    @app.errorhandler(Exception)
    def handle_error(e):
        """
        Always answer the API in JSON.

        Flask's default 500 is an HTML page. The front end calls .json() on it
        and Safari reports "The string did not match the expected pattern",
        which tells the user nothing and points at the wrong thing entirely.
        """
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            if request.path.startswith("/api/"):
                return jsonify({"error": e.description, "status": e.code}), e.code
            return e
        app.logger.exception("Unhandled error on %s", request.path)
        if request.path.startswith("/api/"):
            return jsonify({
                "error": "Something went wrong on the server. If this keeps "
                         "happening, the logs will say why.",
                "detail": str(e)[:300],
            }), 500
        raise e

    @app.get("/")
    def index():
        return render_template("index.html")

    def guard(kind):
        """
        Access gate then rate limit, in that order — a locked door should not
        consume someone's quota. Returns a response to send, or None to proceed.
        """
        if limits.gated():
            return jsonify({"error": "access_required", "gated": True}), 401
        hit = limits.check(get_db(), kind)
        if hit:
            payload, code = hit
            resp = jsonify(payload)
            if payload.get("retry_after"):
                resp.headers["Retry-After"] = str(payload["retry_after"])
            return resp, code
        return None

    @app.post("/api/unlock")
    def api_unlock():
        code = (request.get_json(silent=True) or {}).get("code", "")
        if not limits.ACCESS_CODE or code != limits.ACCESS_CODE:
            return jsonify({"error": "Wrong code"}), 403
        resp = jsonify({"ok": True})
        resp.set_cookie("apparatus_access", limits.ACCESS_CODE,
                        max_age=60 * 60 * 24 * 90, httponly=True, samesite="Lax",
                        secure=request.is_secure)
        return resp

    @app.get("/api/usage")
    def api_usage():
        return jsonify(limits.usage(get_db()))

    def _lang(value=None):
        lang = (value or "en").lower()[:2]
        return lang if lang in ("en", "es") else "en"

    def _translations(lang):
        return app.config["TRANSLATIONS_ES"] if lang == "es" else app.config["TRANSLATIONS"]

    @app.get("/api/passage")
    def api_passage():
        """Just the text. Fast, no model call — used for the reading pane."""
        lang = _lang(request.args.get("lang"))
        try:
            start, end = refs.parse(request.args.get("ref", ""))
        except refs.RefError as e:
            return jsonify({"error": str(e)}), 400
        con = get_db()
        verses = retrieval.passage_text(con, start, end, _translations(lang))
        if not verses:
            t = _translations(lang)[0]
            msg = (f"No se encontró el texto en {t}. Esa traducción no está "
                   f"cargada. Ejecuta:  python -m app.ingest.corpus --bibles {t}"
                   if lang == "es" else
                   f"No text found in {t}. That translation isn't loaded. Run:  "
                   f"python -m app.ingest.corpus --bibles {t}")
            return jsonify({"error": msg}), 404
        return jsonify({
            "ref": refs.range_label(start, end, lang),
            "verses": verses,
            "interlinear": retrieval.interlinear(con, start, end, lang),
        })

    @app.post("/api/study")
    def api_study():
        body = request.get_json(silent=True) or {}
        lang = _lang(body.get("lang"))
        try:
            start, end = refs.parse(body.get("ref", ""))
        except refs.RefError as e:
            return jsonify({"error": str(e)}), 400

        blocked = guard("study")
        if blocked:
            return blocked

        question = (body.get("question") or "").strip() or None
        if question and len(question) > 500:
            return jsonify({"error": "Question is too long — keep it under 500 characters."}), 400

        con = get_db()
        translation = _translations(lang)[0]

        # Resolve to verses that exist before measuring. A whole-chapter
        # reference arrives with a placeholder end; measuring that instead of
        # the real span rejects every chapter, however short.
        start, end, n_verses = retrieval.actual_range(con, start, end, translation)
        if not n_verses:
            msg = ("No se encontró ese pasaje. Revisa la referencia."
                   if lang == "es" else
                   "No text found for that reference. Check the reference.")
            return jsonify({"error": msg}), 404

        MAX_VERSES = 80
        if n_verses > MAX_VERSES:
            where = refs.range_label(start, end, lang)
            msg = (f"{where} tiene {n_verses} versículos — demasiado para un solo "
                   f"estudio. Elige un rango de hasta {MAX_VERSES}, por ejemplo "
                   f"{refs.label(start, lang)}-{refs.unvid(start)[2] + 19}."
                   if lang == "es" else
                   f"{where} has {n_verses} verses — more than one study can cover "
                   f"well. Pick a range of up to {MAX_VERSES}, for example "
                   f"{refs.label(start, lang)}-{refs.unvid(start)[2] + 19}.")
            return jsonify({"error": msg}), 400
        key = hashlib.sha256(
            f"{start}:{end}:{question}:{lang}:{app.config['MODEL']}".encode()
        ).hexdigest()

        if app.config["CACHE_STUDIES"]:
            hit = store.rows(
                con, "SELECT payload FROM studies WHERE cache_key = ?", (key,))
            if hit:
                payload = hit[0]["payload"]
                out = json.loads(payload) if isinstance(payload, str) else payload
                out["cached"] = True
                return jsonify(out)

        t0 = time.time()
        label = refs.range_label(start, end, lang)
        evidence = retrieval.build_evidence(
            con, start, end, question, _translations(lang)
        )
        ev = evidence.to_dict()

        if not ev["passage"]:
            return jsonify({"error": f"No text for {label}. Check the reference."}), 404

        try:
            raw = llm.generate_study(ev, label, question,
                                     model=app.config["MODEL"], lang=lang)
        except (llm.ModelError, RuntimeError) as e:
            return jsonify({"error": str(e)}), 502

        study, report = validate.validate(raw, ev, lang=lang)

        payload = {
            "ref": label,
            "lang": lang,
            "question": question,
            "study": study,
            "evidence": ev,
            "integrity": report.to_dict(),
            "model": app.config["MODEL"],
            "elapsed_ms": int((time.time() - t0) * 1000),
            "cached": False,
        }

        if app.config["CACHE_STUDIES"]:
            store.upsert_study(con, key, start, end, question,
                               json.dumps(payload), app.config["MODEL"],
                               kind="study", title=label, lang=lang)

        return jsonify(payload)

    @app.get("/api/books")
    def api_books():
        """
        Book and chapter index for the picker. Chapter counts come from the
        database rather than a hardcoded table, so they always match the
        translation actually loaded.
        """
        lang = _lang(request.args.get("lang"))
        con = get_db()
        translation = _translations(lang)[0]
        rows = store.rows(con, """
            SELECT vid / 1000000 AS book_ord,
                   (vid % 1000000) / 1000 AS chapter,
                   MAX(vid % 1000) AS verses
            FROM verses WHERE translation = ?
            GROUP BY book_ord, chapter ORDER BY book_ord, chapter
        """, (translation,))

        # An empty result means that translation was never ingested. Say so,
        # with the command that fixes it -- an empty picker with no explanation
        # looks like the app is broken when it is only missing data.
        if not rows:
            loaded = [r["translation"] for r in store.rows(
                con, "SELECT DISTINCT translation FROM verses ORDER BY translation")]
            return jsonify({
                "books": [],
                "translation": translation,
                "missing": True,
                "loaded": loaded,
                "fix": f"python -m app.ingest.corpus --bibles {translation}",
            })

        books, order = {}, []
        for r in rows:
            osis = refs.ORD_TO_OSIS.get(int(r["book_ord"]))
            if not osis:
                continue
            if osis not in books:
                books[osis] = {
                    "osis": osis,
                    "name": refs.book_name(osis, lang),
                    "testament": "ot" if refs.OSIS_TO_ORD[osis] <= 39 else "nt",
                    "chapters": [],
                }
                order.append(osis)
            books[osis]["chapters"].append(
                {"n": int(r["chapter"]), "verses": int(r["verses"])})
        return jsonify({"books": [books[o] for o in order],
                        "translation": translation, "missing": False})

    @app.post("/api/topic")
    def api_topic():
        """
        Topical search. Note this deliberately does not accept a stance --
        see app/topics.py for why the query gets neutralised before retrieval.
        """
        body = request.get_json(silent=True) or {}
        lang = _lang(body.get("lang"))
        topic = (body.get("topic") or "").strip()

        if not topic:
            return jsonify({"error": "Enter a topic." if lang == "en"
                                     else "Escribe un tema."}), 400
        blocked = guard("topic")
        if blocked:
            return blocked
        if len(topic) > 300:
            return jsonify({"error": "Too long — keep it under 300 characters."
                            if lang == "en" else
                            "Demasiado largo — menos de 300 caracteres."}), 400

        con = get_db()
        translation = _translations(lang)[0]
        key = hashlib.sha256(
            f"topic:{topic.lower()}:{lang}:{app.config['MODEL']}".encode()).hexdigest()

        if app.config["CACHE_STUDIES"]:
            hit = store.rows(con, "SELECT payload FROM topics WHERE cache_key = ?", (key,))
            if hit:
                pl = hit[0]["payload"]
                out = json.loads(pl) if isinstance(pl, str) else pl
                out["cached"] = True
                return jsonify(out)

        t0 = time.time()
        try:
            ev = topics.build_topic_evidence(con, topic, translation, lang)
        except Exception as e:
            return jsonify({"error": f"Search failed: {e}"}), 500

        if not ev["passage"]:
            msg = ("No verses matched. Have you run the verse-embedding step? "
                   "python -m app.ingest.embed_verses"
                   if lang == "en" else
                   "No se encontraron versículos. ¿Ejecutaste el paso de embeddings?")
            return jsonify({"error": msg}), 404

        try:
            raw = llm.generate_topic(ev, topic, model=app.config["MODEL"], lang=lang)
        except (llm.ModelError, RuntimeError) as e:
            return jsonify({"error": str(e)}), 502

        study, report = validate.validate_topic(raw, ev, lang=lang)

        payload = {
            "topic": topic,
            "neutralised_query": ev.get("neutralised_query"),
            "lang": lang,
            "study": study,
            "evidence": ev,
            "integrity": report.to_dict(),
            "model": app.config["MODEL"],
            "elapsed_ms": int((time.time() - t0) * 1000),
            "cached": False,
        }

        if app.config["CACHE_STUDIES"]:
            store.upsert_topic(con, key, topic, lang, json.dumps(payload),
                               app.config["MODEL"])

        return jsonify(payload)

    @app.post("/api/sermon")
    def api_sermon():
        """Preaching workbench. Same evidence pipeline as a passage study."""
        body = request.get_json(silent=True) or {}
        lang = _lang(body.get("lang"))
        try:
            start, end = refs.parse(body.get("ref", ""))
        except refs.RefError as e:
            return jsonify({"error": str(e)}), 400

        blocked = guard("sermon")
        if blocked:
            return blocked

        opts = {
            "audience": (body.get("audience") or "").strip()[:120],
            "minutes": body.get("minutes") or 25,
            "occasion": (body.get("occasion") or "").strip()[:120],
            "angle": (body.get("angle") or "").strip()[:200],
        }

        con = get_db()
        translation = _translations(lang)[0]
        start, end, n = retrieval.actual_range(con, start, end, translation)
        if not n:
            return jsonify({"error": f"No text found in {translation}."}), 404
        if n > 80:
            return jsonify({"error": f"{refs.range_label(start, end, lang)} has "
                                     f"{n} verses — pick a shorter passage to preach."}), 400

        key = hashlib.sha256(
            f"sermon:{start}:{end}:{json.dumps(opts, sort_keys=True)}:{lang}:"
            f"{app.config['MODEL']}".encode()).hexdigest()

        if app.config["CACHE_STUDIES"]:
            hit = store.rows(con, "SELECT payload FROM studies WHERE cache_key = ?", (key,))
            if hit:
                pl = hit[0]["payload"]
                out = json.loads(pl) if isinstance(pl, str) else pl
                out["cached"] = True
                return jsonify(out)

        t0 = time.time()
        label = refs.range_label(start, end, lang)
        evidence = retrieval.build_evidence(
            con, start, end, opts["angle"] or None, _translations(lang))
        ev = evidence.to_dict()

        try:
            raw = llm.generate_sermon(ev, label, opts,
                                      model=app.config["MODEL"], lang=lang)
        except (llm.ModelError, RuntimeError) as e:
            return jsonify({"error": str(e)}), 502

        raw["_target_minutes"] = int(opts["minutes"] or 25)
        study, report = validate.validate_sermon(raw, ev, lang=lang)

        payload = {
            "ref": label, "lang": lang, "opts": opts, "kind": "sermon",
            "study": study, "evidence": ev, "integrity": report.to_dict(),
            "model": app.config["MODEL"],
            "elapsed_ms": int((time.time() - t0) * 1000), "cached": False,
        }
        if app.config["CACHE_STUDIES"]:
            store.upsert_study(con, key, start, end, opts["angle"] or opts["audience"],
                               json.dumps(payload), app.config["MODEL"],
                               kind="sermon", title=label, lang=lang)
        return jsonify(payload)

    @app.post("/api/feedback")
    def api_feedback():
        body = request.get_json(silent=True) or {}
        store.execute(get_db(), """
            INSERT INTO feedback (cache_key, kind, ref_label, section, note)
            VALUES (?,?,?,?,?)
        """, (body.get("cache_key"), body.get("kind"), body.get("ref"),
              (body.get("section") or "")[:80], (body.get("note") or "")[:2000]))
        return jsonify({"ok": True})

    @app.get("/api/feedback")
    def api_feedback_list():
        """
        Open reports, newest first. Meant for you, not for users — this is the
        list you hand a pastor when you ask them to check the tool's calls.
        """
        rows = store.rows(get_db(), """
            SELECT * FROM feedback WHERE resolved = 0
            ORDER BY created_at DESC LIMIT 200
        """)
        return jsonify({"items": [dict(r) for r in rows],
                        "count": len(rows)})

    @app.get("/api/notes")
    def api_notes_list():
        """
        Notes for a passage, or the most recent overall.

        Overlap rather than exact match: a note on Romans 5:1-5 should appear
        when you open Romans 5:3, because that is where you left the thought.
        """
        con = get_db()
        ref = request.args.get("ref")
        if ref:
            try:
                start, end = refs.parse(ref)
            except refs.RefError as e:
                return jsonify({"error": str(e)}), 400
            rows = store.rows(con, """
                SELECT * FROM notes WHERE start_vid <= ? AND end_vid >= ?
                ORDER BY updated_at DESC LIMIT 50
            """, (end, start))
        else:
            rows = store.rows(con, """
                SELECT * FROM notes ORDER BY updated_at DESC LIMIT 100
            """)
        return jsonify({"notes": [dict(r) for r in rows]})

    @app.post("/api/notes")
    def api_notes_create():
        body = request.get_json(silent=True) or {}
        text = (body.get("body") or "").strip()
        if not text:
            return jsonify({"error": "Empty note"}), 400
        if len(text) > 20000:
            return jsonify({"error": "Note is too long"}), 400
        lang = _lang(body.get("lang"))
        try:
            start, end = refs.parse(body.get("ref", ""))
        except refs.RefError as e:
            return jsonify({"error": str(e)}), 400

        con = get_db()
        label = refs.range_label(start, end, lang)
        note_id = body.get("id")
        if note_id:
            store.execute(con, """
                UPDATE notes SET body = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (text, note_id))
        else:
            store.execute(con, """
                INSERT INTO notes (start_vid, end_vid, ref_label, body, lang)
                VALUES (?,?,?,?,?)
            """, (start, end, label, text, lang))
        rows = store.rows(con, """
            SELECT * FROM notes WHERE start_vid <= ? AND end_vid >= ?
            ORDER BY updated_at DESC LIMIT 50
        """, (end, start))
        return jsonify({"ok": True, "notes": [dict(r) for r in rows]})

    @app.delete("/api/notes/<int:note_id>")
    def api_notes_delete(note_id):
        store.execute(get_db(), "DELETE FROM notes WHERE id = ?", (note_id,))
        return jsonify({"ok": True})

    @app.get("/api/history")
    def api_history():
        """Everything previously generated, newest first."""
        con = get_db()
        try:
            limit = min(int(request.args.get("limit", 60)), 200)
        except ValueError:
            limit = 60
        saved_only = request.args.get("saved") == "1"
        q = (request.args.get("q") or "").strip()[:80] or None
        rows = store.history(con, limit=limit, saved_only=saved_only, q=q)
        return jsonify({"items": [dict(r) for r in rows]})

    @app.get("/api/history/<key>")
    def api_history_item(key):
        payload = store.load_entry(get_db(), key)
        if payload is None:
            return jsonify({"error": "Not found"}), 404
        out = json.loads(payload) if isinstance(payload, str) else payload
        out["cached"] = True
        return jsonify(out)

    @app.post("/api/history/<key>/save")
    def api_history_save(key):
        body = request.get_json(silent=True) or {}
        ok = store.set_saved(get_db(), key, bool(body.get("saved", True)))
        return (jsonify({"ok": True, "saved": bool(body.get("saved", True))})
                if ok else (jsonify({"error": "Not found"}), 404))

    @app.delete("/api/history/<key>")
    def api_history_delete(key):
        store.delete_entry(get_db(), key)
        return jsonify({"ok": True})

    @app.get("/api/status")
    def api_status():
        """What's actually loaded. Useful when retrieval comes back thin."""
        con = get_db()
        def one(sql):
            return store.one(con, sql) or 0
        sources = store.rows(
            con, "SELECT id, title, author, tradition, license FROM sources ORDER BY id")
        return jsonify({
            "verses": one("SELECT COUNT(*) FROM verses"),
            "translations": [r["translation"] for r in store.rows(
                con, "SELECT DISTINCT translation FROM verses ORDER BY translation")],
            "cross_refs": one("SELECT COUNT(*) FROM cross_refs"),
            "strongs": one("SELECT COUNT(*) FROM strongs"),
            "tagged_words": one("SELECT COUNT(*) FROM verse_words"),
            "interlinear_words": one("SELECT COUNT(*) FROM interlinear"),
            "chunks": one("SELECT COUNT(*) FROM chunks"),
            "verse_vectors": one("SELECT COUNT(*) FROM verse_vecs"),
            "embedded": one("SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL"),
            "sources": sources,
            "traditions": sorted({s["tradition"] for s in sources if s["tradition"]}),
            "corpus_years": _year_range(sources),
            "backend": "postgres" if store.IS_PG else "sqlite",
            "embedding_warning": _embed_warning(con),
        })

    return app
