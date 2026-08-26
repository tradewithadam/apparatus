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
                   max_tokens: int = 6000, lang: str = "en") -> dict:
    tool = {
        "name": "emit_study",
        "description": "Return the completed, fully cited study of the passage.",
        "input_schema": STUDY_SCHEMA,
    }
    try:
        resp = client().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_for(lang),
            tools=[tool],
            tool_choice={"type": "tool", "name": "emit_study"},
            messages=[{"role": "user",
                       "content": build_user_turn(evidence, ref_label, question, lang)}],
        )
    except APIError as e:
        raise ModelError(f"Model request failed: {e}") from e

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            return block.input

    raise ModelError("Model returned no study object")


def stream_note(evidence_summary: str) -> str:
    """Tiny helper for the 'what did we find' line above results."""
    return evidence_summary


def generate_topic(evidence: dict, topic: str,
                   model: str = "claude-sonnet-4-6",
                   max_tokens: int = 7000, lang: str = "en") -> dict:
    from .prompts import TOPIC_SCHEMA, topic_system_for, build_topic_turn
    tool = {
        "name": "emit_topic_study",
        "description": "Return the completed, fully cited topical study.",
        "input_schema": TOPIC_SCHEMA,
    }
    try:
        resp = client().messages.create(
            model=model, max_tokens=max_tokens,
            system=topic_system_for(lang),
            tools=[tool],
            tool_choice={"type": "tool", "name": "emit_topic_study"},
            messages=[{"role": "user", "content": build_topic_turn(evidence, topic, lang)}],
        )
    except APIError as e:
        raise ModelError(f"Model request failed: {e}") from e
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            return block.input
    raise ModelError("Model returned no topic study")


def generate_sermon(evidence: dict, ref_label: str, opts: dict,
                    model: str = "claude-sonnet-4-6",
                    max_tokens: int = 7000, lang: str = "en") -> dict:
    from .prompts import SERMON_SCHEMA, sermon_system_for, build_sermon_turn
    tool = {
        "name": "emit_sermon_workbench",
        "description": "Return the completed, fully cited preaching workbench.",
        "input_schema": SERMON_SCHEMA,
    }
    try:
        resp = client().messages.create(
            model=model, max_tokens=max_tokens,
            system=sermon_system_for(lang),
            tools=[tool],
            tool_choice={"type": "tool", "name": "emit_sermon_workbench"},
            messages=[{"role": "user",
                       "content": build_sermon_turn(evidence, ref_label, opts, lang)}],
        )
    except APIError as e:
        raise ModelError(f"Model request failed: {e}") from e
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            return block.input
    raise ModelError("Model returned no sermon workbench")
