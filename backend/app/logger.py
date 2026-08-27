"""Centralized logging: application/error logs, auth events, user activity.

Three concerns, one entry point, so nobody has to hand-roll `db.insert_one`
calls inside a router or remember to also print something useful to stdout:

- `get_logger(name)` — a standard `logging.Logger` for stdout/stderr output
  (what a process supervisor or `docker logs` captures). Use this the way
  you'd use `print(...)`, just leveled and named.
- `log_auth_event(...)` — persists one row per registration/OTP/login/logout
  event to the `auth_logs` collection, so "why can't this user log in" is a
  database query, not a grep through ephemeral stdout.
- `log_activity(...)` / `log_error(...)` — persists general application
  activity and errors to the `activity_logs` collection (the spec's
  "not just API logging" collection): form submissions, saves, background
  job outcomes, unexpected exceptions.

Nothing here ever accepts or stores a raw password, OTP code, or token —
callers pass identifiers (user_id, email, username) and descriptive
messages/metadata only. `metadata`/`error_details` are stored as-is, so
callers are responsible for not putting secrets in them.
"""
from __future__ import annotations

import logging
import sys
import traceback
import datetime as dt
from typing import Any

from pymongo.database import Database

from .db import next_id

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_configured = False


def _configure_root_once() -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root = logging.getLogger("arena")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.propagate = False
    _configured = True


def get_logger(name: str = "arena") -> logging.Logger:
    """Standard stdout/stderr logger. Use for anything that doesn't need to
    be queryable from the database — startup messages, background-loop
    noise, request tracing."""
    _configure_root_once()
    return logging.getLogger(f"arena.{name}" if name != "arena" else "arena")


_log = get_logger("logger")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def log_auth_event(
    db: Database,
    *,
    event_type: str,
    status: str,
    user_id: int | None = None,
    username: str | None = None,
    email: str | None = None,
    message: str = "",
    ip_address: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record one authentication/onboarding event.

    `event_type` examples: REGISTRATION_STARTED, REGISTRATION_SUCCEEDED,
    OTP_SENT, OTP_SEND_FAILED, OTP_VERIFICATION_SUCCEEDED,
    OTP_VERIFICATION_FAILED, LOGIN_SUCCEEDED, LOGIN_FAILED, LOGOUT,
    TOKEN_REFRESH_SUCCEEDED, TOKEN_REFRESH_FAILED.
    `status` is "SUCCESS" or "FAILED". Never pass a raw password, OTP code,
    or access/refresh token in `message`/`metadata`.
    """
    try:
        db.auth_logs.insert_one(
            {
                "_id": next_id(db, "auth_logs"),
                "event_type": event_type,
                "status": status,
                "user_id": user_id,
                "username": username,
                "email": email,
                "message": message,
                "ip_address": ip_address,
                "metadata": metadata or {},
                "created_at": _now(),
            }
        )
    except Exception:
        # Logging must never be the reason a request fails.
        _log.exception("failed to write auth_logs entry (event_type=%s)", event_type)
    level = logging.INFO if status == "SUCCESS" else logging.WARNING
    _log.log(level, "auth event=%s status=%s user=%s message=%s", event_type, status, username or email or user_id, message)


def log_activity(
    db: Database,
    *,
    action: str,
    status: str = "SUCCESS",
    user_id: int | None = None,
    message: str = "",
    metadata: dict[str, Any] | None = None,
    route: str | None = None,
) -> None:
    """Record a general application/user-activity event — form submits,
    saves, important state changes. Not for auth events; use
    `log_auth_event` for those so they stay easy to filter separately."""
    _write_activity(db, action=action, event_type="ACTIVITY", status=status, user_id=user_id, message=message, metadata=metadata, route=route)
    _log.info("activity action=%s status=%s user=%s message=%s", action, status, user_id, message)


def log_error(
    db: Database,
    *,
    action: str,
    message: str,
    user_id: int | None = None,
    error: BaseException | None = None,
    metadata: dict[str, Any] | None = None,
    route: str | None = None,
) -> None:
    """Record an unexpected backend error/failure. Pass the caught
    exception as `error` to also capture its traceback in `error_details`
    (never pass secrets via `metadata`)."""
    error_details = None
    if error is not None:
        error_details = "".join(traceback.format_exception(type(error), error, error.__traceback__))[-4000:]
    _write_activity(
        db,
        action=action,
        event_type="ERROR",
        status="FAILED",
        user_id=user_id,
        message=message,
        metadata=metadata,
        route=route,
        error_details=error_details,
    )
    _log.error("error action=%s user=%s message=%s", action, user_id, message, exc_info=error is not None)


def _write_activity(
    db: Database,
    *,
    action: str,
    event_type: str,
    status: str,
    user_id: int | None,
    message: str,
    metadata: dict[str, Any] | None,
    route: str | None,
    error_details: str | None = None,
) -> None:
    try:
        db.activity_logs.insert_one(
            {
                "_id": next_id(db, "activity_logs"),
                "user_id": user_id,
                "action": action,
                "event_type": event_type,
                "status": status,
                "message": message,
                "metadata": metadata or {},
                "error_details": error_details,
                "route": route,
                "timestamp": _now(),
            }
        )
    except Exception:
        _log.exception("failed to write activity_logs entry (action=%s)", action)
