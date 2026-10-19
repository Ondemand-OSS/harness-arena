from __future__ import annotations

import datetime as dt
import hashlib

import gridfs
from pymongo import ASCENDING
from pymongo.database import Database

from . import settings

BUCKET_NAME = "connector_files"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def bucket(db: Database) -> gridfs.GridFSBucket:
    return gridfs.GridFSBucket(db, bucket_name=BUCKET_NAME)


def files_collection(db: Database):
    return db[f"{BUCKET_NAME}.files"]


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def ensure_connector_indexes(db: Database) -> None:
    db.connector_users.create_index("email_lower", unique=True)
    db.connector_codes.create_index("expires_at", expireAfterSeconds=0)
    db.connector_tokens.create_index("expires_at", expireAfterSeconds=0)
    db.connector_tokens.create_index("family_id")
    db.connector_tokens.create_index("user_id")
    db.connector_tasks.create_index([("user_id", ASCENDING), ("created_at", ASCENDING)])
    db.connector_tasks.create_index([("status", ASCENDING), ("heartbeat_at", ASCENDING)])
    db.connector_tasks.create_index("finished_at")
    files_collection(db).create_index("metadata.user_id")


def seed_client(db: Database) -> None:
    if not settings.CLIENT_ID or not settings.CLIENT_SECRET:
        return
    db.connector_clients.update_one(
        {"_id": settings.CLIENT_ID},
        {
            "$set": {
                "secret_hash": hash_secret(settings.CLIENT_SECRET),
                "redirect_uris": settings.REDIRECT_URIS,
                "scopes": settings.SCOPES,
                "updated_at": utcnow(),
            },
            "$setOnInsert": {"created_at": utcnow()},
        },
        upsert=True,
    )
