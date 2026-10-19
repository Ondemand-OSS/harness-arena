from __future__ import annotations

import datetime as dt
import hmac
import secrets

from fastapi import Depends, Header, HTTPException
from pymongo.database import Database

from ..db import get_db
from . import settings
from .store import hash_secret, utcnow


def new_token() -> str:
    return secrets.token_urlsafe(48)


def verify_client(db: Database, client_id: str, client_secret: str) -> dict | None:
    client = db.connector_clients.find_one({"_id": client_id or ""})
    if client is None or not client_secret:
        return None
    if not hmac.compare_digest(client["secret_hash"], hash_secret(client_secret)):
        return None
    return client


def issue_tokens(
    db: Database,
    user_id: int,
    client_id: str,
    scope: str,
    family_id: str | None = None,
    refresh_expires_at: dt.datetime | None = None,
) -> dict:
    now = utcnow()
    access = new_token()
    refresh = new_token()
    family = family_id or secrets.token_urlsafe(24)
    refresh_expiry = refresh_expires_at or now + dt.timedelta(seconds=settings.REFRESH_TOKEN_TTL_SECONDS)
    base = {"user_id": user_id, "client_id": client_id, "scope": scope, "family_id": family, "created_at": now}
    db.connector_tokens.insert_many(
        [
            {
                **base,
                "_id": hash_secret(access),
                "kind": "access",
                "expires_at": now + dt.timedelta(seconds=settings.ACCESS_TOKEN_TTL_SECONDS),
            },
            {**base, "_id": hash_secret(refresh), "kind": "refresh", "expires_at": refresh_expiry},
        ]
    )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": settings.ACCESS_TOKEN_TTL_SECONDS,
        "refresh_token": refresh,
        "scope": scope,
    }


def rotate_refresh_token(db: Database, token: str, client_id: str) -> dict | None:
    token_hash = hash_secret(token)
    now = utcnow()
    current = db.connector_tokens.find_one({"_id": token_hash, "kind": "refresh"})
    if current is None or current["client_id"] != client_id:
        return None
    family_id = current["family_id"]
    if current.get("revoked_at") is not None:
        db.connector_tokens.update_many(
            {"family_id": family_id, "revoked_at": {"$exists": False}},
            {"$set": {"revoked_at": now, "revocation_reason": "refresh token reuse detected"}},
        )
        return None
    if current["expires_at"] <= now:
        return None
    consumed = db.connector_tokens.update_one(
        {"_id": token_hash, "revoked_at": {"$exists": False}},
        {"$set": {"revoked_at": now, "revocation_reason": "rotated"}},
    )
    if consumed.modified_count != 1:
        db.connector_tokens.update_many(
            {"family_id": family_id, "revoked_at": {"$exists": False}},
            {"$set": {"revoked_at": now, "revocation_reason": "refresh race or reuse detected"}},
        )
        return None
    db.connector_tokens.update_many(
        {"family_id": family_id, "kind": "access", "revoked_at": {"$exists": False}},
        {"$set": {"revoked_at": now, "revocation_reason": "rotated"}},
    )
    return issue_tokens(
        db,
        current["user_id"],
        current["client_id"],
        current["scope"],
        family_id=family_id,
        refresh_expires_at=current["expires_at"],
    )


def revoke_token(db: Database, token: str, client_id: str) -> None:
    current = db.connector_tokens.find_one({"_id": hash_secret(token)}, {"family_id": 1, "client_id": 1})
    if current is None or current["client_id"] != client_id:
        return
    db.connector_tokens.update_many(
        {"family_id": current["family_id"], "revoked_at": {"$exists": False}},
        {"$set": {"revoked_at": utcnow(), "revocation_reason": "revoked by client"}},
    )


def _unauthorized(detail: str = "invalid or expired token") -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def require_connector_user(scope: str):
    def dependency(authorization: str | None = Header(default=None), db: Database = Depends(get_db)) -> dict:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise _unauthorized("missing bearer token")
        token = authorization[7:].strip()
        record = db.connector_tokens.find_one({"_id": hash_secret(token), "kind": "access"})
        if record is None or record.get("revoked_at") is not None or record["expires_at"] <= utcnow():
            raise _unauthorized()
        if scope not in record["scope"].split():
            raise HTTPException(status_code=403, detail=f"token lacks required scope: {scope}")
        user = db.connector_users.find_one({"_id": record["user_id"]})
        if user is None:
            raise _unauthorized()
        return user

    return dependency
