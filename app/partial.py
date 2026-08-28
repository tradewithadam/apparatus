"""
Parsing JSON that hasn't finished arriving.

The model streams its tool input as a JSON string, a few characters at a time.
Halfway through it looks like:

    {"summary": "Paul says that those justified by faith have peace",
     "settled": [{"claim": "Justification comes through fai

which is not valid JSON and will not parse. But the completed parts of it are
perfectly usable — the summary is done, and the first settled claim is nearly
so. Waiting for the closing brace before showing anything is what makes a
90-second study feel like a hang.

So: close whatever is open, parse, and use only the parts that were genuinely
finished. The last element of any array is assumed incomplete and dropped,
because a truncated claim is worse than a slightly late one.
"""
import json


def _close(buf: str) -> str | None:
    """
    Append whatever brackets and quotes are needed to make `buf` parseable.

    Walks the string tracking string state and escape state, because a brace
    inside a quoted value must not be counted as structure — a claim
    containing "{" would otherwise throw the stack off and silently corrupt
    every subsequent parse.
    """
    stack = []
    in_string = False
    escaped = False

    for ch in buf:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_string:
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()

    out = buf
    if escaped:
        out = out[:-1]              # a dangling backslash breaks the parse
    if in_string:
        out += '"'

    # A trailing comma or a key with no value yet — trim back to something
    # that can legally close.
    out = out.rstrip()
    while out and out[-1] in ",:":
        out = out[:-1].rstrip()
        if out and out[-1] == '"':
            # we just cut a key; drop the key too
            depth = 0
            for k in range(len(out) - 1, -1, -1):
                if out[k] == '"' and (k == 0 or out[k - 1] != "\\"):
                    depth += 1
                    if depth == 2:
                        out = out[:k].rstrip().rstrip(",")
                        break

    for opener in reversed(stack):
        out += "}" if opener == "{" else "]"

    return out or None


def parse_partial(buf: str) -> dict:
    """
    Best-effort object from an incomplete JSON string. Returns {} if nothing
    coherent can be recovered yet.
    """
    if not buf or not buf.lstrip().startswith("{"):
        return {}
    closed = _close(buf)
    if not closed:
        return {}
    try:
        val = json.loads(closed)
        return val if isinstance(val, dict) else {}
    except json.JSONDecodeError:
        return {}


def settled_items(obj: dict, key: str, still_arriving: bool = True) -> list:
    """
    Elements of obj[key] that are safe to treat as finished.

    Drops the final element while the stream is live: it is the one currently
    being written, and half a claim is worse than a claim that shows up a
    second later.
    """
    items = obj.get(key)
    if not isinstance(items, list):
        return []
    if still_arriving and items:
        return items[:-1]
    return items


def scalar_ready(obj: dict, key: str, later_keys: tuple[str, ...]) -> bool:
    """
    Is a non-array field finished?

    A string value is only known to be complete once a later field has begun,
    because until then the model may still be adding to it.
    """
    if key not in obj:
        return False
    return any(k in obj for k in later_keys)
