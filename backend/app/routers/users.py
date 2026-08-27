from __future__ import annotations

import datetime as dt
import hmac
import os
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from ..db import get_db, next_id
from ..logger import log_activity, log_auth_event
from ..users import (
    EMAIL_VERIFICATION_TTL_SECONDS,
    MIN_PASSWORD_LENGTH,
    OTP_MAX_ATTEMPTS,
    REFRESH_TOKEN_TTL_SECONDS,
    check_login_rate_limit,
    check_otp_resend_rate_limit,
    current_user,
    hash_password,
    hash_verification_code,
    issue_access_token,
    issue_refresh_session,
    is_admin,
    require_user,
    require_arena_admin,
    revoke_refresh_session,
    rotate_refresh_session,
    send_signup_code,
    verify_password,
)

router = APIRouter(prefix="/api/users", tags=["users"])

USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,32}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
REFRESH_COOKIE_NAME = "arena_refresh_token"
REFRESH_COOKIE_PATH = "/api/users/session"
# Optional refresh-token header for deployments that cannot use credentialed cookies.
REFRESH_HEADER_NAME = "x-refresh-token"
REFRESH_COOKIE_SECURE = os.environ.get("ARENA_COOKIE_SECURE", "true").strip().lower() not in {"0", "false", "no"}
REFRESH_COOKIE_SAMESITE = os.environ.get(
    "ARENA_REFRESH_COOKIE_SAMESITE", "none" if REFRESH_COOKIE_SECURE else "lax"
).strip().lower()
if REFRESH_COOKIE_SAMESITE not in {"lax", "strict", "none"}:
    REFRESH_COOKIE_SAMESITE = "none" if REFRESH_COOKIE_SECURE else "lax"


def _allowed_origins() -> set[str]:
    configured = {origin.strip().rstrip("/") for origin in os.environ.get("ARENA_CORS_ORIGINS", "").split(",") if origin.strip()}
    return {"http://localhost:5173", "http://127.0.0.1:5173", *configured}


def _require_allowed_browser_origin(request: Request) -> None:
    """Protect cookie-issuing/consuming endpoints from cross-site requests."""
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in _allowed_origins():
        raise HTTPException(status_code=403, detail="request origin is not allowed")


class SignupIn(BaseModel):
    username: str
    email: str
    password: str
    display_name: str = ""


class VerifySignupIn(BaseModel):
    email: str
    code: str


class LoginIn(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    email: str = ""
    display_name: str
    avatar_key: str = ""
    is_admin: bool = False
    # Whether this user has their own OnDemand API key set — never the key
    # itself. OnDemand is the one harness that runs on the signed-in user's
    # own credential rather than a shared provider profile; the UI uses
    # this to gate offering OnDemand in a battle. See PUT /me/ondemand-key.
    has_ondemand_api_key: bool = False


class SessionOut(BaseModel):
    access_token: str
    access_token_expires_at: int
    user: UserOut
    # Only populated for the flows that actually mint/rotate a session
    # (signup/verify, login, session/refresh) — see REFRESH_HEADER_NAME
    # above for why this exists alongside the HttpOnly cookie.
    refresh_token: str = ""


class OndemandKeyIn(BaseModel):
    api_key: str = ""  # blank clears the stored key


class UserLimitOverrideIn(BaseModel):
    task_submission_limit: int | None = Field(default=None, ge=1, le=1000)
    max_active_tasks: int | None = Field(default=None, ge=1, le=100)


class UserLimitOverrideOut(BaseModel):
    id: int
    username: str
    display_name: str
    avatar_key: str = ""
    task_submission_limit: int | None = None
    max_active_tasks: int | None = None


def _out(doc: dict) -> UserOut:
    return UserOut(
        id=doc["_id"],
        username=doc["username"],
        email=doc.get("email", ""),
        display_name=doc.get("display_name") or doc["username"],
        avatar_key=doc.get("avatar_key", ""),
        is_admin=is_admin(doc),
        has_ondemand_api_key=bool(doc.get("ondemand_api_key")),
    )


def _limit_out(doc: dict) -> UserLimitOverrideOut:
    return UserLimitOverrideOut(
        id=doc["_id"],
        username=doc["username"],
        display_name=doc.get("display_name") or doc["username"],
        avatar_key=doc.get("avatar_key", ""),
        task_submission_limit=doc.get("task_submission_limit"),
        max_active_tasks=doc.get("max_active_tasks"),
    )


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        max_age=REFRESH_TOKEN_TTL_SECONDS,
        httponly=True,
        secure=REFRESH_COOKIE_SECURE,
        samesite=REFRESH_COOKIE_SAMESITE,
        path=REFRESH_COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        httponly=True,
        secure=REFRESH_COOKIE_SECURE,
        samesite=REFRESH_COOKIE_SAMESITE,
        path=REFRESH_COOKIE_PATH,
    )


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _incoming_refresh_token(request: Request) -> str:
    """Header first (works regardless of the platform's CORS behavior),
    falling back to the cookie for anything still relying on it."""
    return request.headers.get(REFRESH_HEADER_NAME, "").strip() or request.cookies.get(REFRESH_COOKIE_NAME, "")


def _new_session(user: dict, response: Response, db: Database) -> SessionOut:
    access_token, access_expires_at = issue_access_token(user["_id"], db)
    refresh_token, _refresh_expires_at = issue_refresh_session(user["_id"], db)
    _set_refresh_cookie(response, refresh_token)
    return SessionOut(
        access_token=access_token,
        access_token_expires_at=access_expires_at,
        user=_out(user),
        refresh_token=refresh_token,
    )


@router.post("/signup/request-verification")
def request_signup_verification(body: SignupIn, request: Request, db: Database = Depends(get_db)):
    """Email a code before creating an account for this address."""
    _require_allowed_browser_origin(request)
    username = body.username.strip()
    email = body.email.strip().lower()
    ip = _client_ip(request)
    log_auth_event(db, event_type="REGISTRATION_STARTED", status="SUCCESS", username=username, email=email, ip_address=ip)
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(
            status_code=400,
            detail="username must be 3-32 characters, letters/numbers/dot/underscore/hyphen only",
        )
    if not EMAIL_RE.fullmatch(email):
        raise HTTPException(status_code=400, detail="enter a valid email address")
    if len(body.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if db.users.find_one({"username_lower": username.lower()}, {"_id": 1}):
        raise HTTPException(status_code=409, detail="that username is taken")
    if db.users.find_one({"email_lower": email}, {"_id": 1}):
        raise HTTPException(status_code=409, detail="that email is already registered")
    check_otp_resend_rate_limit(db, email)

    code = f"{secrets.randbelow(1_000_000):06d}"
    # The Mongo client is configured with tz_aware=True (see mongo.py), so
    # every datetime read back from a document comes back tz-aware UTC —
    # `now` here must match that representation, or comparing it against a
    # value just read from Mongo (see verify_signup below) raises
    # "can't compare offset-naive and offset-aware datetimes".
    now = dt.datetime.now(dt.timezone.utc)
    pending = {
        "_id": email,
        "username": username,
        "username_lower": username.lower(),
        "email": email,
        "email_lower": email,
        "display_name": body.display_name.strip() or username,
        "password_hash": hash_password(body.password),
        "code_hash": hash_verification_code(code),
        "attempts": 0,
        "expires_at": now + dt.timedelta(seconds=EMAIL_VERIFICATION_TTL_SECONDS),
        "created_at": now,
    }
    db.pending_signups.replace_one({"_id": email}, pending, upsert=True)
    try:
        send_signup_code(email, code)
    except RuntimeError as exc:
        db.pending_signups.delete_one({"_id": email, "code_hash": pending["code_hash"]})
        log_auth_event(db, event_type="OTP_SEND_FAILED", status="FAILED", username=username, email=email, message=str(exc), ip_address=ip)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    log_auth_event(db, event_type="OTP_SENT", status="SUCCESS", username=username, email=email, ip_address=ip)
    return {"detail": "verification code sent", "expires_in_seconds": EMAIL_VERIFICATION_TTL_SECONDS}


@router.post("/signup/verify", response_model=SessionOut)
def verify_signup(body: VerifySignupIn, request: Request, response: Response, db: Database = Depends(get_db)):
    _require_allowed_browser_origin(request)
    email = body.email.strip().lower()
    ip = _client_ip(request)
    pending = db.pending_signups.find_one({"_id": email})
    # See request_signup_verification — `now` must be tz-aware to compare
    # against `expires_at`, which comes back tz-aware from Mongo.
    now = dt.datetime.now(dt.timezone.utc)
    if pending is None or pending.get("expires_at", now) <= now:
        log_auth_event(db, event_type="OTP_VERIFICATION_FAILED", status="FAILED", email=email, message="code expired or not found", ip_address=ip)
        raise HTTPException(status_code=400, detail="verification code expired; request a new code")
    if pending.get("attempts", 0) >= OTP_MAX_ATTEMPTS:
        db.pending_signups.delete_one({"_id": email})
        log_auth_event(db, event_type="OTP_VERIFICATION_FAILED", status="FAILED", email=email, message="too many attempts", ip_address=ip)
        raise HTTPException(status_code=429, detail="too many incorrect attempts; request a new code")
    if not hmac.compare_digest(str(pending.get("code_hash", "")), hash_verification_code(body.code.strip())):
        db.pending_signups.update_one({"_id": email}, {"$inc": {"attempts": 1}})
        log_auth_event(
            db,
            event_type="OTP_VERIFICATION_FAILED",
            status="FAILED",
            username=pending.get("username"),
            email=email,
            message="incorrect code",
            ip_address=ip,
            metadata={"attempts": pending.get("attempts", 0) + 1},
        )
        raise HTTPException(status_code=400, detail="incorrect verification code")

    user_id = next_id(db, "users")
    doc = {
        "_id": user_id,
        "username": pending["username"],
        "username_lower": pending["username_lower"],
        "email": pending["email"],
        "email_lower": pending["email_lower"],
        "display_name": pending["display_name"],
        "password_hash": pending["password_hash"],
        "avatar_key": "",
        "email_verified_at": now,
        "created_at": now,
    }
    try:
        db.users.insert_one(doc)
    except DuplicateKeyError:
        log_auth_event(db, event_type="REGISTRATION_FAILED", status="FAILED", username=pending.get("username"), email=email, message="duplicate username or email", ip_address=ip)
        raise HTTPException(status_code=409, detail="that username or email is already registered")
    db.pending_signups.delete_one({"_id": email})

    log_auth_event(db, event_type="OTP_VERIFICATION_SUCCEEDED", status="SUCCESS", user_id=user_id, username=doc["username"], email=email, ip_address=ip)
    log_auth_event(db, event_type="REGISTRATION_SUCCEEDED", status="SUCCESS", user_id=user_id, username=doc["username"], email=email, ip_address=ip)
    return _new_session(doc, response, db)


@router.post("/login", response_model=SessionOut)
def login(body: LoginIn, request: Request, response: Response, db: Database = Depends(get_db)):
    _require_allowed_browser_origin(request)
    username_lower = body.username.strip().lower()
    ip = _client_ip(request)
    check_login_rate_limit(db, username_lower)
    user = db.users.find_one({"username_lower": username_lower})
    # Same message either way so this can't be used to enumerate usernames.
    if user is None or not verify_password(body.password, user["password_hash"]):
        log_auth_event(db, event_type="LOGIN_FAILED", status="FAILED", username=username_lower, message="incorrect username or password", ip_address=ip)
        raise HTTPException(status_code=401, detail="incorrect username or password")
    log_auth_event(db, event_type="LOGIN_SUCCEEDED", status="SUCCESS", user_id=user["_id"], username=username_lower, ip_address=ip)
    return _new_session(user, response, db)


@router.post("/session/refresh", response_model=SessionOut)
def refresh_session(request: Request, response: Response, db: Database = Depends(get_db)):
    _require_allowed_browser_origin(request)
    refresh_token = _incoming_refresh_token(request)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="refresh session is missing")
    rotated = rotate_refresh_session(refresh_token, db)
    if rotated is None:
        _clear_refresh_cookie(response)
        log_auth_event(db, event_type="TOKEN_REFRESH_FAILED", status="FAILED", message="refresh session invalid or expired", ip_address=_client_ip(request))
        raise HTTPException(status_code=401, detail="refresh session is invalid or expired")
    user_id, replacement_token, _refresh_expires_at = rotated
    user = db.users.find_one({"_id": user_id})
    if user is None:
        revoke_refresh_session(replacement_token, db)
        _clear_refresh_cookie(response)
        log_auth_event(db, event_type="TOKEN_REFRESH_FAILED", status="FAILED", user_id=user_id, message="user no longer exists", ip_address=_client_ip(request))
        raise HTTPException(status_code=401, detail="refresh session is invalid")
    # Deliberately not logging the success case: the access token is
    # short-lived (10 min) by design, so any open tab silently refreshes it
    # in the background all day — logging every one of those would make
    # auth_logs grow ~150x faster than every other event combined, for a
    # row that says nothing more than "still logged in". Login/logout
    # already capture the events that matter; failures here (expired reuse,
    # deleted user) still get logged above since those ARE diagnostic.
    access_token, access_expires_at = issue_access_token(user_id, db)
    _set_refresh_cookie(response, replacement_token)
    return SessionOut(
        access_token=access_token,
        access_token_expires_at=access_expires_at,
        user=_out(user),
        refresh_token=replacement_token,
    )


@router.post("/session/logout", status_code=204)
def logout_session(request: Request, response: Response, db: Database = Depends(get_db)):
    _require_allowed_browser_origin(request)
    refresh_token = _incoming_refresh_token(request)
    if refresh_token:
        revoke_refresh_session(refresh_token, db)
    _clear_refresh_cookie(response)
    log_auth_event(db, event_type="LOGOUT", status="SUCCESS", ip_address=_client_ip(request))


@router.get("/admin/limits", response_model=list[UserLimitOverrideOut])
def list_user_limit_overrides(db: Database = Depends(get_db), _admin: dict = Depends(require_arena_admin)):
    return [_limit_out(user) for user in db.users.find().sort("username_lower", 1) if not is_admin(user)]


@router.put("/admin/limits/{user_id}", response_model=UserLimitOverrideOut)
def update_user_limit_override(
    user_id: int,
    body: UserLimitOverrideIn,
    db: Database = Depends(get_db),
    _admin: dict = Depends(require_arena_admin),
):
    user = db.users.find_one({"_id": user_id})
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if is_admin(user):
        raise HTTPException(status_code=400, detail="the arena admin already has unlimited task limits")

    values = body.model_dump()
    set_fields = {key: value for key, value in values.items() if value is not None}
    unset_fields = {key: "" for key, value in values.items() if value is None}
    operation = {}
    if set_fields:
        operation["$set"] = set_fields
    if unset_fields:
        operation["$unset"] = unset_fields
    db.users.update_one({"_id": user_id}, operation)
    updated = db.users.find_one({"_id": user_id})
    log_activity(
        db,
        action="USER_LIMIT_OVERRIDE_UPDATE",
        user_id=_admin["_id"],
        message=f"updated task limits for {updated['username']}",
        metadata={"target_user_id": user_id, **values},
        route=f"/api/users/admin/limits/{user_id}",
    )
    return _limit_out(updated)


@router.get("/me", response_model=UserOut | None)
def me(user: dict | None = Depends(current_user)):
    """Who the caller is, or null. Never 401s — the UI uses this to decide
    whether to show a sign-in prompt."""
    return _out(user) if user else None


@router.get("/me/strict", response_model=UserOut)
def me_strict(user: dict = Depends(require_user)):
    return _out(user)


@router.put("/me/ondemand-key", response_model=UserOut)
def set_ondemand_key(body: OndemandKeyIn, db: Database = Depends(get_db), user: dict = Depends(require_user)):
    """Each user brings their own OnDemand key — it has no relation to the
    shared provider profiles (config.py) every other harness uses. Blank
    clears it (matches ProviderConfigIn's "blank means keep/clear" idiom
    used elsewhere for secrets)."""
    key = body.api_key.strip()
    db.users.update_one({"_id": user["_id"]}, {"$set": {"ondemand_api_key": key}})
    log_activity(
        db,
        action="ONDEMAND_KEY_UPDATE",
        user_id=user["_id"],
        message="user cleared their OnDemand API key" if not key else "user set their OnDemand API key",
        route="/api/users/me/ondemand-key",
    )
    return _out({**user, "ondemand_api_key": key})
