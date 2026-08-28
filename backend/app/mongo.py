"""MongoDB connection helpers."""
from __future__ import annotations

import os

from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient
from pymongo.database import Database
from pymongo.errors import OperationFailure

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB_NAME = os.environ.get("MONGODB_DB_NAME", "harness_arena")
# How long auth/activity log rows live before Mongo's TTL index prunes
# them automatically. These logs exist for incident investigation and
# abuse detection (see check_login_rate_limit / check_otp_resend_rate_limit
# in users.py, which query auth_logs directly) — not as a permanent audit
# trail — so bounding them keeps the collections from growing unbounded.
# Override via env if you need longer retention for compliance reasons.
AUTH_LOG_RETENTION_DAYS = int(os.environ.get("AUTH_LOG_RETENTION_DAYS", "90"))
ACTIVITY_LOG_RETENTION_DAYS = int(os.environ.get("ACTIVITY_LOG_RETENTION_DAYS", "30"))

_client: MongoClient | None = None

# Explicit pool bounds. pymongo's default maxPoolSize (100) is sized for a
# dedicated cluster; on a shared/low-tier Atlas cluster a handful of app
# instances each opening up to 100 connections can approach the cluster's
# connection cap on its own, and idle connections keep counting against it
# forever without a maxIdleTimeMS. One process should never need more than a
# small multiple of ARENA_MAX_CONCURRENT_RUNS (see runner.py) plus request
# traffic, so bound it and let idle connections close themselves down.
MONGODB_MAX_POOL_SIZE = int(os.environ.get("MONGODB_MAX_POOL_SIZE", "20"))
MONGODB_MIN_POOL_SIZE = int(os.environ.get("MONGODB_MIN_POOL_SIZE", "0"))
MONGODB_MAX_IDLE_TIME_MS = int(os.environ.get("MONGODB_MAX_IDLE_TIME_MS", "60000"))


def get_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=8000,
            tz_aware=True,
            maxPoolSize=MONGODB_MAX_POOL_SIZE,
            minPoolSize=MONGODB_MIN_POOL_SIZE,
            maxIdleTimeMS=MONGODB_MAX_IDLE_TIME_MS,
        )
    return _client


def close_client() -> None:
    """Release the pooled connections on process shutdown."""
    global _client
    if _client is not None:
        _client.close()
        _client = None


def get_db() -> Database:
    """FastAPI dependency — yields the one database this app is scoped to."""
    return get_client()[MONGODB_DB_NAME]


def next_id(db: Database, counter_name: str) -> int:
    """Return the next numeric ID for a collection."""
    doc = db.counters.find_one_and_update(
        {"_id": counter_name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    return doc["seq"]


def _ensure_ttl_index(collection, field: str, expire_after_seconds: int) -> None:
    try:
        collection.create_index(field, expireAfterSeconds=expire_after_seconds)
    except OperationFailure as exc:
        if exc.code != 85:
            raise
        collection.database.command(
            "collMod",
            collection.name,
            index={"keyPattern": {field: 1}, "expireAfterSeconds": expire_after_seconds},
        )


def ensure_indexes(db: Database) -> None:
    """Create required database indexes."""
    db.users.create_index("username_lower", unique=True)
    db.users.create_index("email_lower", unique=True, sparse=True)
    # One pending signup per email. Mongo removes expired verification codes.
    db.pending_signups.create_index("expires_at", expireAfterSeconds=0)
    db.refresh_sessions.create_index("expires_at", expireAfterSeconds=0)
    db.refresh_sessions.create_index("user_id")
    db.refresh_sessions.create_index("family_id")
    db.scores.create_index([("task_id", ASCENDING), ("harness_key", ASCENDING), ("user_id", ASCENDING), ("provider_config_id", ASCENDING)], unique=True)
    db.scores.create_index("user_id")
    db.scores.create_index([("task_id", ASCENDING), ("provider_config_id", ASCENDING)])
    # elo.compute_leaderboard's whole-site case (no category/task_ids) filters
    # only is_deleted, then sorts by judged_at — none of the compound indexes
    # above start with is_deleted, so that was a full collection scan plus an
    # in-memory sort on every leaderboard cache miss.
    db.scores.create_index([("is_deleted", ASCENDING), ("judged_at", ASCENDING)])
    db.judge_verdicts.create_index([("task_id", ASCENDING), ("harness_key", ASCENDING)], unique=True)
    db.runs.create_index([("task_id", ASCENDING), ("harness_key", ASCENDING), ("source", ASCENDING)])
    db.runs.create_index([("task_id", ASCENDING), ("is_deleted", ASCENDING), ("provider_config_id", ASCENDING)])
    # The admin runs overview and stats.py's "done" count filter/sort by
    # status across the WHOLE collection, not scoped to one task.
    db.runs.create_index([("status", ASCENDING), ("_id", ASCENDING)])
    # Lease reconciliation (runner.reconcile_orphaned_runs / its periodic
    # sweep) queries pending+running rows by heartbeat age, collection-wide.
    db.runs.create_index([("status", ASCENDING), ("heartbeat_at", ASCENDING)])
    db.batches.create_index([("status", ASCENDING), ("heartbeat_at", ASCENDING)])
    # rate_limit.require_no_active_runs checks "does this user already have
    # a pending/running run" on every submission — scoped by submitter, not
    # by task, so the task_id-prefixed indexes above can't serve it.
    db.runs.create_index([("submitted_by_user_id", ASCENDING), ("status", ASCENDING)])
    db.deliverables.create_index("run_id")
    # Real reference-file bytes attached to a task (routers/tasks.py's
    # reference-files endpoints) — looked up by task_id on every run, same
    # reasoning as the deliverables index above.
    db.task_reference_files.create_index("task_id")
    db.tasks.create_index("category")
    db.category_reviews.create_index("status")
    # list_tasks/list_categories/list_groups/stats all filter on is_deleted
    # across every task on every page load.
    db.tasks.create_index("is_deleted")
    # scores.py's compare() gates identity reveal on judge_verdicts too,
    # and judge_stats.py scans this collection filtered by is_deleted alone
    # (harness stats) or (harness_key, is_deleted) (a harness's profile
    # page) — neither is a usable prefix of the (task_id, harness_key)
    # unique index above.
    db.judge_verdicts.create_index("is_deleted")
    db.judge_verdicts.create_index([("harness_key", ASCENDING), ("is_deleted", ASCENDING)])
    # Every task submission checks "how many has this user made in the last
    # 24h?" (see rate_limit.py), so that lookup gets its own index rather
    # than scanning an ever-growing collection on every submit.
    db.task_submissions.create_index([("user_id", ASCENDING), ("created_at", ASCENDING)])
    # auth_logs: queried both to investigate one user's history and to
    # enforce login/OTP-resend rate limits ("how many FAILED LOGIN_FAILED
    # events for this username in the last 15 minutes"), so those lookups
    # need their own indexes rather than scanning the whole log. Also
    # TTL'd — these are for recent incident/abuse investigation, not
    # permanent audit, so Mongo prunes them automatically rather than
    # letting the collection grow forever (see AUTH_LOG_RETENTION_DAYS /
    # ACTIVITY_LOG_RETENTION_DAYS below).
    db.auth_logs.create_index([("username", ASCENDING), ("event_type", ASCENDING), ("created_at", ASCENDING)])
    db.auth_logs.create_index([("email", ASCENDING), ("event_type", ASCENDING), ("created_at", ASCENDING)])
    db.auth_logs.create_index([("user_id", ASCENDING), ("created_at", ASCENDING)])
    _ensure_ttl_index(db.auth_logs, "created_at", AUTH_LOG_RETENTION_DAYS * 86400)
    # activity_logs: same shape of lookup — one user's activity, or the most
    # recent errors across everyone. Same TTL reasoning as auth_logs.
    db.activity_logs.create_index([("user_id", ASCENDING), ("timestamp", ASCENDING)])
    db.activity_logs.create_index([("event_type", ASCENDING), ("timestamp", ASCENDING)])
    _ensure_ttl_index(db.activity_logs, "timestamp", ACTIVITY_LOG_RETENTION_DAYS * 86400)
