"""
Rate limiting and the access gate.

Three layers, because they fail differently:

  per-IP hourly      stops one person hammering the endpoint
  per-IP daily       stops one person grinding through the Bible overnight
  GLOBAL daily       stops everyone, together, from emptying your account

The global cap is the one that actually protects the card. Per-IP limits are
trivially defeated by anyone who wants to — a handful of proxies and each one
gets a fresh allowance. A hard ceiling on total model calls per day cannot be
routed around, and the worst case becomes a known number instead of an open
question. Set it to what you are willing to lose in a day.

Counters live in SQLite rather than memory because gunicorn runs more than one
worker, and per-process counters mean each worker enforcing its own limit —
two workers, double the traffic, and no error anywhere to tell you.

This is a doorknob, not a deadbolt. It is sized for "I put a link in a text
message and something went wrong", which is the realistic threat, not for a
determined attacker.
"""
import os
import time

from flask import request, jsonify

from . import store

RATE_PER_HOUR = int(os.environ.get("RATE_PER_HOUR", "20"))
RATE_PER_DAY = int(os.environ.get("RATE_PER_DAY", "60"))
GLOBAL_DAILY_CAP = int(os.environ.get("GLOBAL_DAILY_CAP", "500"))
ACCESS_CODE = os.environ.get("ACCESS_CODE", "").strip()

HOUR = 3600
DAY = 86400


def client_ip() -> str:
    """
    Render and every other proxy put the real address in X-Forwarded-For and
    their own in REMOTE_ADDR. Reading REMOTE_ADDR behind a proxy gives every
    visitor the same identity, so the first person to hit the limit locks out
    the world.
    """
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.remote_addr or "unknown")[:64]


def _count(con, bucket: str, since: int) -> int:
    return store.one(con, """
        SELECT COUNT(*) FROM rate_events WHERE bucket = ? AND ts > ?
    """, (bucket, since)) or 0


def record(con, bucket: str, kind: str):
    store.execute(con, "INSERT INTO rate_events (bucket, kind, ts) VALUES (?,?,?)",
                  (bucket, kind, int(time.time())))


def prune(con):
    """Drop anything older than the longest window. Cheap; runs opportunistically."""
    store.execute(con, "DELETE FROM rate_events WHERE ts < ?",
                  (int(time.time()) - DAY - HOUR,))


def check(con, kind: str, bucket: str | None = None):
    """
    Returns None when allowed, or (payload, status) to return to the caller.

    `bucket` lets a signed-in user be limited by account rather than address —
    fairer on a church wifi where everyone shares one IP, and it means the
    admin usage page reflects people rather than networks.
    """
    now = int(time.time())
    ip = bucket or client_ip()

    if now % 37 == 0:              # roughly 1 request in 37 tidies up
        prune(con)

    used_global = _count(con, "global", now - DAY)
    if GLOBAL_DAILY_CAP and used_global >= GLOBAL_DAILY_CAP:
        return ({
            "error": "This app has hit its daily limit and will reset within "
                     "24 hours. Nothing is broken — it is a spending guard.",
            "limit": "global",
        }, 429)

    hourly = _count(con, ip, now - HOUR)
    if RATE_PER_HOUR and hourly >= RATE_PER_HOUR:
        return ({
            "error": f"You've run {hourly} studies in the last hour, which is "
                     f"the limit. Try again a little later.",
            "limit": "hourly", "retry_after": HOUR,
        }, 429)

    daily = _count(con, ip, now - DAY)
    if RATE_PER_DAY and daily >= RATE_PER_DAY:
        return ({
            "error": f"You've run {daily} studies today, which is the limit. "
                     f"It resets 24 hours after your first one.",
            "limit": "daily", "retry_after": DAY,
        }, 429)

    record(con, ip, kind)
    record(con, "global", kind)
    return None


def gated() -> bool:
    """Is an access code required, and does this request lack it?"""
    if not ACCESS_CODE:
        return False
    return (request.cookies.get("adfontes_access")
            or request.cookies.get("apparatus_access")) != ACCESS_CODE


def usage(con) -> dict:
    now = int(time.time())
    return {
        "global_today": _count(con, "global", now - DAY),
        "global_cap": GLOBAL_DAILY_CAP,
        "your_hour": _count(con, client_ip(), now - HOUR),
        "hour_cap": RATE_PER_HOUR,
        "your_day": _count(con, client_ip(), now - DAY),
        "day_cap": RATE_PER_DAY,
        "gated": bool(ACCESS_CODE),
    }
