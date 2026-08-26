"""
Embeddings.

Local sentence-transformers by default: free, offline, no per-query cost, and
plenty good for retrieving 19th-century commentary prose. Swap to Voyage by
setting EMBED_BACKEND=voyage if you later want the quality bump.

Whichever you pick, stay on it. Vectors from different models are not
comparable, so switching backends means re-embedding the whole corpus.
"""
import os
import numpy as np

BACKEND = os.environ.get("EMBED_BACKEND", "local")
LOCAL_MODEL = os.environ.get("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
VOYAGE_MODEL = os.environ.get("VOYAGE_MODEL", "voyage-3.5-lite")

# Voyage was acquired by MongoDB, so there are now two endpoints and the key
# you hold only works against one of them:
#
#   key from voyageai.com          -> https://api.voyageai.com/v1
#   key from Atlas (AI Model APIs) -> https://ai.mongodb.com/v1
#
# A key from the wrong side returns 401 with nothing in the message about
# endpoints, which is a confusing hour. Set VOYAGE_BASE_URL to match wherever
# you made the key.
VOYAGE_BASE_URL = os.environ.get("VOYAGE_BASE_URL", "https://api.voyageai.com/v1").rstrip("/")

_model = None


def _local():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(LOCAL_MODEL)
    return _model


def _voyage_embed(texts: list[str], input_type: str) -> list[list[float]]:
    """
    Direct REST call rather than the voyageai client library.

    The library hardcodes api.voyageai.com, which locks out anyone whose key
    came from Atlas. Both endpoints speak the same request shape, so a plain
    POST works against either and drops a dependency.
    """
    import json
    import urllib.error
    import urllib.request

    key = os.environ.get("VOYAGE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("VOYAGE_API_KEY is not set")

    body = json.dumps({
        "input": texts,
        "model": VOYAGE_MODEL,
        "input_type": input_type,
    }).encode()
    req = urllib.request.Request(
        f"{VOYAGE_BASE_URL}/embeddings", data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code in (401, 403):
            raise RuntimeError(
                f"Voyage rejected the key against {VOYAGE_BASE_URL}. A key made "
                f"in MongoDB Atlas needs VOYAGE_BASE_URL=https://ai.mongodb.com/v1; "
                f"a key from voyageai.com needs the default. ({detail})"
            ) from e
        raise RuntimeError(f"Voyage error {e.code}: {detail}") from e

    return [d["embedding"] for d in sorted(data["data"], key=lambda x: x["index"])]


def embed_texts(texts: list[str], input_type: str = "document") -> np.ndarray:
    if not texts:
        return np.zeros((0, dim()), dtype=np.float32)
    if BACKEND == "voyage":
        # Batches are capped; the API rejects oversized requests outright.
        vecs = []
        for i in range(0, len(texts), 96):
            vecs.extend(_voyage_embed(texts[i:i + 96], input_type))
        return np.array(vecs, dtype=np.float32)
    return np.array(
        _local().encode(texts, batch_size=64, show_progress_bar=False,
                        normalize_embeddings=True),
        dtype=np.float32,
    )


def embed_query(text: str) -> np.ndarray:
    return embed_texts([text], input_type="query")[0]


_dim = None


def dim() -> int:
    """
    Detected from a real embedding rather than hardcoded, because model
    dimensions change between versions and a stale constant here produces a
    pgvector column of the wrong width -- which fails loudly at migration time
    and silently corrupts nothing, but wastes an hour.
    """
    global _dim
    if _dim is None:
        _dim = int(embed_texts(["dimension probe"]).shape[1])
    return _dim


def model_id() -> str:
    """Backend and model, without touching the network."""
    return f"{BACKEND}:{VOYAGE_MODEL if BACKEND == 'voyage' else LOCAL_MODEL}"


def signature() -> str:
    """
    Identifies the vector space: backend + model + dimensions.

    Calls dim(), which for a hosted backend means an API request. Use it when
    recording what produced a set of vectors — never on a read path.
    """
    return f"{model_id()}:{dim()}"


def stamp(con):
    """Record which model wrote these vectors. Called after any embedding run."""
    con.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('embedding', ?)",
        (signature(),))
    con.commit()


def check(con) -> str | None:
    """
    Warning if the stored vectors came from a different model, else None.

    Compares backend and model name only — deliberately not dimensions, since
    obtaining those means embedding a probe string. That made the *check* for
    whether the embedding service was usable itself require the embedding
    service, so a misconfigured or rate-limited key produced an API call and an
    exception on every single search. A read path must never spend a request.
    """
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'embedding'").fetchone()
    except Exception:
        return None
    if not row:
        return None
    stored = row[0] if not hasattr(row, "keys") else row["value"]
    if stored.rsplit(":", 1)[0] == model_id():
        return None
    return (f"Vectors in this database were made by {stored}, but "
            f"EMBED_BACKEND is now {model_id()}. Semantic search is disabled. "
            f"Re-run the embedding steps with the current backend, or set "
            f"EMBED_BACKEND back to what built it.")
