"""User accounts: password hashing and session tokens.

Anyone can sign up. Signing in is required to submit a benchmark or to
judge — those actions write to the shared leaderboard, so they need to be
attributable to someone. Browsing (tasks, leaderboard, battle log) stays
open.

Password storage uses PBKDF2-HMAC-SHA256 from the standard library, so
there's no extra dependency to keep patched. Hashes are stored as
`pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>` — self-describing, so
the iteration count can be raised later without invalidating old hashes.

Separate from auth.py, which guards the single-password *admin* surface
(provider keys, harness registration). These are ordinary end users.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
import datetime as dt

from fastapi import Depends, Header, HTTPException
from pymongo import ReturnDocument
from pymongo.database import Database

from .db import get_db

ITERATIONS = 240_000
ACCESS_TOKEN_TTL_SECONDS = 10 * 60
REFRESH_TOKEN_TTL_SECONDS = 15 * 24 * 60 * 60
MIN_PASSWORD_LENGTH = 8
EMAIL_VERIFICATION_TTL_SECONDS = 15 * 60
# Brute-force protection on the OTP code itself: once a pending signup's
# wrong-attempt count hits this, the code is dead even though it hasn't
# expired yet — the caller has to request a new one.
OTP_MAX_ATTEMPTS = 5
# OTP resend throttling, counted from auth_logs' OTP_SENT rows for the
# email (see check_otp_resend_rate_limit).
OTP_RESEND_LIMIT = 3
OTP_RESEND_WINDOW_HOURS = 1
# Login brute-force protection, counted from auth_logs' LOGIN_FAILED rows
# for the username (see check_login_rate_limit).
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_MINUTES = 15
# The arena owner manages shared, free-to-use provider profiles. Keep this
# as a username rather than a mutable database flag so ownership stays
# explicit when restoring or moving the database. Case-insensitive to match
# account lookup and uniqueness.
ARENA_ADMIN_USERNAME = os.environ.get("ARENA_ADMIN_USERNAME", "ondemand").strip().lower()

SESSION_SECRET_ENV = "ARENA_SESSION_SECRET"


def hash_verification_code(code: str) -> str:
    """Keep short-lived signup codes out of Mongo in plain text."""
    return hashlib.sha256(code.encode()).hexdigest()


def send_signup_code(email: str, code: str) -> None:
    """Send a signup code using SendGrid's HTTPS API."""
    api_key = os.environ.get("SENDGRID_API_KEY", "").strip()
    sender = os.environ.get("SENDGRID_FROM_EMAIL", "").strip()
    if not api_key or not sender:
        raise RuntimeError("email verification is not configured")

    # Imported here so a missing optional dependency turns into the same
    # safe delivery failure instead of preventing the whole API from booting.
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        message = Mail(
            from_email=sender,
            to_emails=email,
            subject="Your Harness Arena verification code",
            plain_text_content=(
                f"Your Harness Arena verification code is: {code}\n\n"
                f"It expires in {EMAIL_VERIFICATION_TTL_SECONDS // 60} minutes. If you did not request it, ignore this email."
            ),
        )
        response = SendGridAPIClient(api_key).send(message)
        if not 200 <= response.status_code < 300:
            raise RuntimeError("SendGrid rejected the verification email")
    except Exception as exc:
        if isinstance(exc, RuntimeError) and str(exc) == "SendGrid rejected the verification email":
            raise
        raise RuntimeError("could not send verification email") from exc


_signing_key_cache: bytes | None = None


def _signing_key(db: Database) -> bytes:
    """Return a restart-stable session key without exposing it over HTTP.

    Deployments may set `ARENA_SESSION_SECRET`. Otherwise a local arena gets
    one generated once in its own Mongo database, so a normal backend restart
    cannot leave the UI appearing signed in while protected API calls fail.

    Memoized in-process: this is called on nearly every request (token
    sign/verify), and the value never changes once it exists — env var is
    fixed for the process lifetime, and the DB fallback only ever creates
    the document once ($setOnInsert). Without this, every single request
    paid for a Mongo find_one_and_update just to re-read a constant. A race
    on first call is harmless: find_one_and_update's upsert is atomic per
    document, so concurrent callers all converge on the same stored value.
    """
    global _signing_key_cache
    if _signing_key_cache is not None:
        return _signing_key_cache
    configured = os.environ.get(SESSION_SECRET_ENV)
    if configured:
        _signing_key_cache = configured.encode()
        return _signing_key_cache
    setting = db.arena_settings.find_one_and_update(
        {"_id": "session_signing_key"},
        {"$setOnInsert": {"value": secrets.token_urlsafe(48)}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    _signing_key_cache = setting["value"].encode()
    return _signing_key_cache


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    try:
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


def is_admin(user: dict | None) -> bool:
    return bool(user and user.get("username_lower", user.get("username", "").lower()) == ARENA_ADMIN_USERNAME)


def issue_access_token(user_id: int, db: Database) -> tuple[str, int]:
    """Issue a short-lived token that the browser keeps in memory only."""
    expires_at = int(time.time()) + ACCESS_TOKEN_TTL_SECONDS
    payload = f"{user_id}:{expires_at}:{secrets.token_urlsafe(12)}"
    signature = hmac.new(_signing_key(db), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}", expires_at


def _refresh_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_refresh_session(user_id: int, db: Database) -> tuple[str, dt.datetime]:
    """Create a 15-day refresh-token family, storing only its token hash."""
    now = dt.datetime.now(dt.timezone.utc)
    expires_at = now + dt.timedelta(seconds=REFRESH_TOKEN_TTL_SECONDS)
    token = secrets.token_urlsafe(48)
    db.refresh_sessions.insert_one(
        {
            "_id": _refresh_hash(token),
            "user_id": user_id,
            "family_id": secrets.token_urlsafe(24),
            "created_at": now,
            "expires_at": expires_at,
        }
    )
    return token, expires_at


def rotate_refresh_session(token: str, db: Database) -> tuple[int, str, dt.datetime] | None:
    """Consume a refresh token and rotate its session family."""
    token_hash = _refresh_hash(token)
    now = dt.datetime.now(dt.timezone.utc)
    current = db.refresh_sessions.find_one({"_id": token_hash})
    if current is None:
        return None
    family_id = current.get("family_id")
    if current.get("revoked_at") is not None:
        if family_id:
            db.refresh_sessions.update_many(
                {"family_id": family_id, "revoked_at": {"$exists": False}},
                {"$set": {"revoked_at": now, "revocation_reason": "refresh token reuse detected"}},
            )
        return None
    expires_at = current.get("expires_at", now)
    if expires_at <= now:
        return None

    replacement = secrets.token_urlsafe(48)
    replacement_hash = _refresh_hash(replacement)
    consumed = db.refresh_sessions.update_one(
        {"_id": token_hash, "revoked_at": {"$exists": False}},
        {
            "$set": {
                "revoked_at": now,
                "revocation_reason": "rotated",
                "replaced_by_hash": replacement_hash,
            }
        },
    )
    if consumed.modified_count != 1:
        if family_id:
            db.refresh_sessions.update_many(
                {"family_id": family_id, "revoked_at": {"$exists": False}},
                {"$set": {"revoked_at": now, "revocation_reason": "refresh race or reuse detected"}},
            )
        return None

    db.refresh_sessions.insert_one(
        {
            "_id": replacement_hash,
            "user_id": current["user_id"],
            "family_id": family_id,
            "created_at": now,
            # Rotation does not extend the original 15-day login session.
            "expires_at": expires_at,
        }
    )
    return current["user_id"], replacement, expires_at


def revoke_refresh_session(token: str, db: Database) -> None:
    current = db.refresh_sessions.find_one({"_id": _refresh_hash(token)}, {"family_id": 1})
    if current is None:
        return
    query = {"family_id": current["family_id"]} if current.get("family_id") else {"_id": current["_id"]}
    db.refresh_sessions.update_many(
        {**query, "revoked_at": {"$exists": False}},
        {"$set": {"revoked_at": dt.datetime.now(dt.timezone.utc), "revocation_reason": "logout"}},
    )


def _user_id_from_token(token: str, db: Database) -> int | None:
    payload, _, signature = token.rpartition(".")
    if not payload or not signature:
        return None
    expected = hmac.new(_signing_key(db), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    parts = payload.split(":")
    if len(parts) != 3:
        return None
    user_part, expiry_part, _token_id = parts
    try:
        if int(expiry_part) <= int(time.time()):
            return None
        return int(user_part)
    except ValueError:
        return None


def current_user(
    x_user_token: str | None = Header(default=None),
    db: Database = Depends(get_db),
):
    """Resolves the signed-in user (as the raw Mongo document), or None. Use
    for endpoints that behave differently when signed in but don't require
    it."""
    if not x_user_token:
        return None
    user_id = _user_id_from_token(x_user_token, db)
    if user_id is None:
        return None
    return db.users.find_one({"_id": user_id})


def require_user(
    x_user_token: str | None = Header(default=None),
    db: Database = Depends(get_db),
):
    """Dependency for endpoints that must be attributable to a person —
    judging and submitting benchmarks."""
    user = current_user(x_user_token, db)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in to continue")
    return user


def require_arena_admin(user: dict = Depends(require_user)):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="OnDemand admin access required")
    return user


def check_login_rate_limit(db: Database, username_lower: str) -> None:
    """Raise 429 if this username has racked up too many failed logins
    recently. Counted from auth_logs rather than a separate counter
    collection — login attempts are already logged there, so there's
    nothing else to keep in sync."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=LOGIN_WINDOW_MINUTES)
    recent_failures = db.auth_logs.count_documents(
        {"username": username_lower, "event_type": "LOGIN_FAILED", "created_at": {"$gte": since}}
    )
    if recent_failures >= LOGIN_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail=f"too many failed login attempts; try again in {LOGIN_WINDOW_MINUTES} minutes",
        )


def check_otp_resend_rate_limit(db: Database, email: str) -> None:
    """Raise 429 if this email has requested too many verification codes
    recently (covers both first-send and resend, since they're the same
    endpoint)."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=OTP_RESEND_WINDOW_HOURS)
    recent_sends = db.auth_logs.count_documents(
        {"email": email, "event_type": "OTP_SENT", "status": "SUCCESS", "created_at": {"$gte": since}}
    )
    if recent_sends >= OTP_RESEND_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"too many verification codes requested; try again in {OTP_RESEND_WINDOW_HOURS} hour(s)",
        )
