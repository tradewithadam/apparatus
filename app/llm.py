"""
Model call.

Uses tool-use with a forced tool choice rather than "please return JSON" in the
prompt. The API then guarantees well-formed JSON matching the schema, so you
never write a regex to pull a JSON object out of a markdown fence again.
"""
import json
import os

from anthropic import Anthropic, APIError

from .prompts import system_for, STUDY_SCHEMA, build_user_turn

_client = None

# Filled in by each generate_* call so the caller can log real token counts
# rather than guessing from text length.
LAST_USAGE = {"input_tokens": 0, "output_tokens": 0,
              "cache_read": 0, "cache_write": 0, "model": None}


def _record(resp, model):
    u = getattr(resp, "usage", None)
    LAST_USAGE.update(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
        model=model,
    )
    return dict(LAST_USAGE)


def client() -> Anthropic:
    global _client
    if _client is None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = Anthropic(api_key=key)
    return _client


class ModelError(RuntimeError):
    pass


def generate_study(evidence: dict, ref_label: str, question: str | None,
                   model: str = "claude-sonnet-4-6",
                   max_tokens: int = 6000, lang: str = "en",
                   lens: str | None = None) -> dict:
    tool = {
        "name": "emit_study",
        "description": "Return the completed, fully cited study of the passage.",
        "input_schema": STUDY_SCHEMA,
    }
    try:
        resp = client().messages.create(
            model=model,
            max_tokens=max_tokens,
            # The system prompt and tool schema are identical on every call and
            # run to a few thousand tokens. Marking them cacheable means
            # Anthropic charges a cache *read* rather than fresh input for them
            # — roughly a tenth the price — on every study after the first
            # within the cache window. The evidence packet still costs full
            # price, because it genuinely differs each time.
            system=[{"type": "text", "text": system_for(lang, lens),
                     "cache_control": {"type": "ephemeral"}}],
            tools=[tool],
            tool_choice={"type": "tool", "name": "emit_study"},
            messages=[{"role": "user",
                       "content": build_user_turn(evidence, ref_label, question, lang)}],
        )
    except APIError as e:
        raise ModelError(f"Model request failed: {e}") from e

    _record(resp, model)
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            return block.input

    raise ModelError("Model returned no study object")


def stream_note(evidence_summary: str) -> str:
    """Tiny helper for the 'what did we find' line above results."""
    return evidence_summary


def generate_topic(evidence: dict, topic: str,
                   model: str = "claude-sonnet-4-6",
                   max_tokens: int = 7000, lang: str = "en",
                   lens: str | None = None) -> dict:
    from .prompts import TOPIC_SCHEMA, topic_system_for, build_topic_turn
    tool = {
        "name": "emit_topic_study",
        "description": "Return the completed, fully cited topical study.",
        "input_schema": TOPIC_SCHEMA,
    }
    try:
        resp = client().messages.create(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": topic_system_for(lang, lens),
                     "cache_control": {"type": "ephemeral"}}],
            tools=[tool],
            tool_choice={"type": "tool", "name": "emit_topic_study"},
            messages=[{"role": "user", "content": build_topic_turn(evidence, topic, lang)}],
        )
    except APIError as e:
        raise ModelError(f"Model request failed: {e}") from e
    _record(resp, model)
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            return block.input
    raise ModelError("Model returned no topic study")


def generate_sermon(evidence: dict, ref_label: str, opts: dict,
                    model: str = "claude-sonnet-4-6",
                    max_tokens: int = 7000, lang: str = "en",
                    lens: str | None = None) -> dict:
    from .prompts import SERMON_SCHEMA, sermon_system_for, build_sermon_turn
    tool = {
        "name": "emit_sermon_workbench",
        "description": "Return the completed, fully cited preaching workbench.",
        "input_schema": SERMON_SCHEMA,
    }
    try:
        resp = client().messages.create(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": sermon_system_for(lang, lens),
                     "cache_control": {"type": "ephemeral"}}],
            tools=[tool],
            tool_choice={"type": "tool", "name": "emit_sermon_workbench"},
            messages=[{"role": "user",
                       "content": build_sermon_turn(evidence, ref_label, opts, lang)}],
        )
    except APIError as e:
        raise ModelError(f"Model request failed: {e}") from e
    _record(resp, model)
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            return block.input
    raise ModelError("Model returned no sermon workbench")


def stream_study(evidence: dict, ref_label: str, question: str | None,
                 model: str = "claude-sonnet-4-6", max_tokens: int = 6000,
                 lang: str = "en", lens: str | None = None):
    """
    Yield the study as it is written.

    Anthropic streams tool input as `input_json_delta` events — raw JSON text,
    a few characters at a time. Accumulating that and parsing it leniently lets
    finished sections reach the reader while the model is still working on the
    rest. Total time is unchanged; the wait stops feeling like a hang.

    Yields ("partial", obj) repeatedly, then ("final", obj).
    """
    from .prompts import system_for, STUDY_SCHEMA, build_user_turn
    from .partial import parse_partial

    tool = {
        "name": "emit_study",
        "description": "Return the completed, fully cited study of the passage.",
        "input_schema": STUDY_SCHEMA,
    }
    buf = ""
    try:
        with client().messages.stream(
            model=model, max_tokens=max_tokens,
            # The system prompt and tool schema are identical on every call and
            # run to a few thousand tokens. Marking them cacheable means
            # Anthropic charges a cache *read* rather than fresh input for them
            # — roughly a tenth the price — on every study after the first
            # within the cache window. The evidence packet still costs full
            # price, because it genuinely differs each time.
            system=[{"type": "text", "text": system_for(lang, lens),
                     "cache_control": {"type": "ephemeral"}}],
            tools=[tool],
            tool_choice={"type": "tool", "name": "emit_study"},
            messages=[{"role": "user",
                       "content": build_user_turn(evidence, ref_label, question, lang)}],
        ) as s:
            for event in s:
                if getattr(event, "type", "") == "content_block_delta":
                    d = getattr(event, "delta", None)
                    chunk = getattr(d, "partial_json", None)
                    if chunk:
                        buf += chunk
                        obj = parse_partial(buf)
                        if obj:
                            yield ("partial", obj)
            final = s.get_final_message()
            _record(final, model)
    except APIError as e:
        raise ModelError(f"Model request failed: {e}") from e

    for block in final.content:
        if getattr(block, "type", None) == "tool_use":
            yield ("final", block.input)
            return
    raise ModelError("Model returned no study object")
