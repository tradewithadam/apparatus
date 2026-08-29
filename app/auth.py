"""
Accounts.

Deliberately small: email, password, session cookie. No OAuth, no email
verification flow, no password reset by mail — those need an email service and
this does not have one yet. What it does have is the part that matters, which
is that one person's notes are theirs.

A NOTE ON WHAT IS AND IS NOT PER-USER

  shared   the study cache (`studies`, `topics`)
  personal history, notes, feedback, usage

Keeping the cache global is what makes this affordable. Ten people studying
John 3:16 is one model call and nine free reads. Giving each account its own
cache would multiply the bill by the user count for no benefit — nobody's
study of John 3:16 is private, it is the same study. What is private is that
*you* studied it, and what you wrote about it.

SECURITY, HONESTLY

Passwords are hashed with scrypt via werkzeug. Sessions are random 32-byte
tokens in an httponly cookie, stored server-side so they can be revoked.
Login is rate limited per email and per IP.

This is appropriate for a tool shared with a church. It is not a bank. If this
ever holds anything more sensitive than study notes, get someone who does
security for a living to look at it.
"""
import os
import secrets
import time

from werkzeug.security import check_password_hash, generate_password_hash

from . import store

SESSION_COOKIE = "adfontes_session"
# The name before the rename. Still read, never written — so a rename does not
# sign out everyone who was already logged in, and the old cookie ages out on
# its own rather than needing a migration.
LEGACY_SESSION_COOKIE = "apparatus_session"
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "90"))
INVITE_CODE = os.environ.get("INVITE_CODE", "").strip()
# Break-glass recovery. Set it in the host's environment, use it, then unset it.
# Only someone who can already change the server's configuration can use this,
# which is the same person who could edit the database directly — so it grants
# no access they did not already have, and it saves them from being locked out
# of their own admin account with no way back in.
RECOVERY_TOKEN = os.environ.get("RECOVERY_TOKEN", "").strip()
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()

# Two ceilings, deliberately far apart.
#
# The per-email limit stops someone guessing one account's password. The per-IP
# limit stops someone spraying many accounts from one machine. They are not the
# same problem and should not share a number: a church, a household, or an
# office all share one address, so a tight IP limit means one person fumbling
# their own password locks out everyone around them.
LOGIN_TRIES = 8             # per email address
LOGIN_TRIES_IP = 40         # per address — many people legitimately share one
LOGIN_WINDOW = 900          # 15 minutes


class AuthError(Exception):
    """Message is safe to show the user."""


def _now() -> int:
    return int(time.time())


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()[:200]


def validate_password(pw: str):
    if len(pw or "") < 8:
        raise AuthError("Password must be at least 8 characters.")
    if len(pw) > 200:
        raise AuthError("That password is too long.")


def validate_email(email: str):
    e = normalize_email(email)
    if "@" not in e or "." not in e.split("@")[-1] or len(e) < 6:
        raise AuthError("That doesn't look like an email address.")
    return e


def register(con, email: str, password: str, name: str = "",
             invite: str = "") -> dict:
    if INVITE_CODE and (invite or "").strip() != INVITE_CODE:
        raise AuthError("That invite code isn't right.")
    e = validate_email(email)
    validate_password(password)

    if store.rows(con, "SELECT 1 AS x FROM users WHERE email = ?", (e,)):
        # Deliberately the same wording a person would see if they simply
        # forgot they had signed up. Saying "no such account" on login and
        # "already exists" here lets anyone enumerate who has registered.
        raise AuthError("That email is already registered. Try signing in.")

    first = not store.rows(con, "SELECT 1 AS x FROM users LIMIT 1")
    is_admin = 1 if (first or (ADMIN_EMAIL and e == ADMIN_EMAIL)) else 0

    store.execute(con, """
        INSERT INTO users (email, password_hash, name, is_admin, created_at, last_seen)
        VALUES (?,?,?,?,?,?)
    """, (e, generate_password_hash(password), (name or "").strip()[:80],
          is_admin, _now(), _now()))
    return get_by_email(con, e)


def _too_many_attempts(con, bucket: str, limit: int) -> bool:
    n = store.one(con, """
        SELECT COUNT(*) FROM login_attempts WHERE bucket = ? AND ts > ?
    """, (bucket, _now() - LOGIN_WINDOW)) or 0
    return n >= limit


def _note_attempt(con, bucket: str):
    store.execute(con, "INSERT INTO login_attempts (bucket, ts) VALUES (?,?)",
                  (bucket, _now()))


def login(con, email: str, password: str, ip: str = "") -> dict:
    e = normalize_email(email)
    for bucket, limit in ((f"e:{e}", LOGIN_TRIES), (f"i:{ip}", LOGIN_TRIES_IP)):
        if _too_many_attempts(con, bucket, limit):
            raise AuthError("Too many attempts. Wait fifteen minutes and try again.")

    rows = store.rows(con, "SELECT * FROM users WHERE email = ?", (e,))
    ok = bool(rows) and check_password_hash(rows[0]["password_hash"], password or "")
    if not ok:
        _note_attempt(con, f"e:{e}")
        _note_attempt(con, f"i:{ip}")
        raise AuthError("Email or password is wrong.")

    store.execute(con, "UPDATE users SET last_seen = ? WHERE id = ?",
                  (_now(), rows[0]["id"]))
    return dict(rows[0])


def start_session(con, user_id: int, user_agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    store.execute(con, """
        INSERT INTO sessions (token, user_id, created_at, expires_at, user_agent)
        VALUES (?,?,?,?,?)
    """, (token, user_id, _now(), _now() + SESSION_DAYS * 86400,
          (user_agent or "")[:200]))
    return token


def end_session(con, token: str):
    if token:
        store.execute(con, "DELETE FROM sessions WHERE token = ?", (token,))


def user_for_token(con, token: str) -> dict | None:
    if not token:
        return None
    rows = store.rows(con, """
        SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token = ? AND s.expires_at > ?
    """, (token, _now()))
    if not rows:
        return None
    u = dict(rows[0])
    # Cheap last-seen tracking; a minute of granularity is plenty and avoids
    # a write on every single request.
    if _now() - (u.get("last_seen") or 0) > 60:
        store.execute(con, "UPDATE users SET last_seen = ? WHERE id = ?",
                      (_now(), u["id"]))
    return u


def get_by_email(con, email: str) -> dict | None:
    rows = store.rows(con, "SELECT * FROM users WHERE email = ?",
                      (normalize_email(email),))
    return dict(rows[0]) if rows else None


def public(u: dict | None) -> dict | None:
    """Never let the hash out of this module."""
    if not u:
        return None
    return {
        "id": u["id"], "email": u["email"], "name": u.get("name") or "",
        "is_admin": bool(u.get("is_admin")),
        "created_at": u.get("created_at"),
    }


def change_password(con, user_id: int, current: str, new: str):
    rows = store.rows(con, "SELECT password_hash FROM users WHERE id = ?", (user_id,))
    if not rows or not check_password_hash(rows[0]["password_hash"], current or ""):
        raise AuthError("Current password is wrong.")
    validate_password(new)
    store.execute(con, "UPDATE users SET password_hash = ? WHERE id = ?",
                  (generate_password_hash(new), user_id))
    # Every other session is invalidated: a password change usually means the
    # old one is suspect, and leaving other devices signed in defeats the point.
    store.execute(con, "DELETE FROM sessions WHERE user_id = ?", (user_id,))


def admin_reset(con, admin: dict, user_id: int) -> str:
    """
    Issue a temporary password for another account. Returns it once.

    Deliberately not "set a password an admin chose": generating a random one
    means the admin never learns a password the user might reuse elsewhere,
    and it cannot be a guessable pattern like the person's name plus a year.
    The user changes it after signing in.

    Every session for that account is ended. If someone is locked out because
    their account was taken, leaving the intruder signed in defeats the reset.
    """
    if not admin.get("is_admin"):
        raise AuthError("Not allowed.")
    rows = store.rows(con, "SELECT id, email, is_admin FROM users WHERE id = ?", (user_id,))
    if not rows:
        raise AuthError("No such account.")
    target = dict(rows[0])

    # An admin resetting another admin is how one compromised admin account
    # becomes all of them. Only self-reset is allowed at that level.
    if target.get("is_admin") and target["id"] != admin["id"]:
        raise AuthError("You can't reset another admin's password.")

    temp = f"{secrets.choice(WORDS)}-{secrets.choice(WORDS)}-{secrets.randbelow(9000) + 1000}"
    store.execute(con, "UPDATE users SET password_hash = ? WHERE id = ?",
                  (generate_password_hash(temp), user_id))
    store.execute(con, "DELETE FROM sessions WHERE user_id = ?", (user_id,))
    # Clear the lockout, or the failed attempts that prompted the reset keep
    # them out for another fifteen minutes and the reset appears not to work.
    store.execute(con, "DELETE FROM login_attempts WHERE bucket = ?",
                  (f"e:{target['email']}",))
    # Their address is unknown here, and it was almost certainly filled by the
    # same failed attempts. Since the IP ceiling exists to stop spraying across
    # many accounts rather than to protect one, clearing recent address history
    # on an explicit admin action is the right trade.
    store.execute(con, "DELETE FROM login_attempts WHERE bucket LIKE 'i:%' AND ts > ?",
                  (_now() - LOGIN_WINDOW,))
    return temp


# Readable temporary passwords. Someone is going to read one of these aloud
# over the phone, so no ambiguous characters and no words that sound alike.
WORDS = [
    "anchor", "beacon", "cedar", "canyon", "harbor", "lantern", "meadow",
    "orchard", "prairie", "quarry", "ridge", "summit", "thicket", "valley",
    "willow", "amber", "cobalt", "crimson", "indigo", "olive", "saffron",
    "granite", "marble", "timber", "compass", "kettle", "saddle", "trellis",
]


def recover(con, token: str, email: str, password: str) -> dict:
    """
    Reset any account's password, or create it if it is missing.

    Creating when absent is deliberate: the common way to end up locked out is
    the database being replaced by a fresh copy, which takes the accounts with
    it. In that case there is nothing to reset, and an error saying "no such
    account" would be accurate and useless.
    """
    if not RECOVERY_TOKEN:
        raise AuthError("Recovery is not enabled.")
    if not secrets.compare_digest(token or "", RECOVERY_TOKEN):
        raise AuthError("Wrong recovery token.")

    e = validate_email(email)
    validate_password(password)
    rows = store.rows(con, "SELECT id FROM users WHERE email = ?", (e,))
    if rows:
        uid = rows[0]["id"]
        store.execute(con, "UPDATE users SET password_hash = ?, is_admin = 1 WHERE id = ?",
                      (generate_password_hash(password), uid))
        action = "reset"
    else:
        store.execute(con, """
            INSERT INTO users (email, password_hash, name, is_admin, created_at, last_seen)
            VALUES (?,?,?,1,?,?)
        """, (e, generate_password_hash(password), "", _now(), _now()))
        uid = store.one(con, "SELECT id FROM users WHERE email = ?", (e,))
        action = "created"

    store.execute(con, "DELETE FROM sessions WHERE user_id = ?", (uid,))
    store.execute(con, "DELETE FROM login_attempts", ())
    return {"action": action, "user_id": uid,
            "total_users": store.one(con, "SELECT COUNT(*) FROM users") or 0}


def diagnose(con, token: str) -> dict:
    """Is the account actually there? Answers the question behind the lockout."""
    if not RECOVERY_TOKEN or not secrets.compare_digest(token or "", RECOVERY_TOKEN):
        raise AuthError("Wrong recovery token.")
    users = store.rows(con, """
        SELECT id, email, is_admin, created_at, last_seen FROM users ORDER BY id
    """)
    return {
        "user_count": len(users),
        "users": [{"id": u["id"], "email": u["email"],
                   "is_admin": bool(u["is_admin"])} for u in users],
        "recent_failed_logins": store.one(con, """
            SELECT COUNT(*) FROM login_attempts WHERE ts > ?
        """, (_now() - LOGIN_WINDOW,)) or 0,
    }


def prune(con):
    store.execute(con, "DELETE FROM sessions WHERE expires_at < ?", (_now(),))
    store.execute(con, "DELETE FROM login_attempts WHERE ts < ?",
                  (_now() - LOGIN_WINDOW * 4,))
