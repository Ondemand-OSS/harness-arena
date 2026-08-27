"""Per-user submission quota.

Running a task is expensive now that the harnesses spawn real `claude`/
`codex` CLI subprocesses against a real (or OnDemand-funded free) API key,
so an ordinary account can only submit a bounded number of tasks per day.
The arena admin (see users.is_admin) is exempt — they're the one funding
and operating the arena.

Only *submissions* are counted, one document per task the user asked to
run. Retrying a failed run deliberately does not go through here: a run
that errored produced nothing usable, so charging quota for it would
penalize the user for the arena's own failure (see routers/runs.py's
retry endpoint).

This is a rolling window, not a calendar day: a submission stops counting
exactly TASK_SUBMISSION_WINDOW_HOURS after it was made.
"""
from __future__ import annotations

import datetime as dt

from fastapi import HTTPException
from pymongo.database import Database

from .db import next_id
from .users import is_admin

TASK_SUBMISSION_LIMIT = 10
TASK_SUBMISSION_WINDOW_HOURS = 24
MAX_ACTIVE_TASKS_PER_USER = 1


def task_submission_limit(user: dict) -> int:
    return max(1, int(user.get("task_submission_limit") or TASK_SUBMISSION_LIMIT))


def max_active_tasks(user: dict) -> int:
    return max(1, int(user.get("max_active_tasks") or MAX_ACTIVE_TASKS_PER_USER))


def _window_start() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=TASK_SUBMISSION_WINDOW_HOURS)


def used_in_window(db: Database, user_id: int) -> int:
    return db.task_submissions.count_documents({"user_id": user_id, "created_at": {"$gte": _window_start()}})


def remaining_quota(db: Database, user: dict) -> int:
    return max(0, task_submission_limit(user) - used_in_window(db, user["_id"]))


def _next_reset(db: Database, user_id: int) -> dt.datetime | None:
    """When the oldest still-counting submission ages out — i.e. when the
    user gets at least one slot back."""
    oldest = db.task_submissions.find_one(
        {"user_id": user_id, "created_at": {"$gte": _window_start()}}, sort=[("created_at", 1)]
    )
    if oldest is None:
        return None
    return oldest["created_at"] + dt.timedelta(hours=TASK_SUBMISSION_WINDOW_HOURS)


def require_quota(db: Database, user: dict, task_count: int) -> None:
    """Raise 429 if this user can't afford `task_count` more task runs."""
    if is_admin(user):
        return
    limit = task_submission_limit(user)
    remaining = remaining_quota(db, user)
    if task_count <= remaining:
        return
    reset_at = _next_reset(db, user["_id"])
    when = f" Try again after {reset_at:%Y-%m-%d %H:%M UTC}." if reset_at else ""
    raise HTTPException(
        status_code=429,
        detail=(
            f"Daily limit reached: {limit} task runs per "
            f"{TASK_SUBMISSION_WINDOW_HOURS} hours. You have {remaining} left but asked for "
            f"{task_count}.{when}"
        ),
    )


def require_no_active_runs(db: Database, user: dict, task_count: int = 1) -> None:
    """Raise 409 when this user has reached the active-task limit.

    Each concurrent run is a real, resource-costly subprocess (or a live
    OnDemand session) — see runner.py's MAX_CONCURRENT_RUNS. Without this,
    one user submitting task after task before any of them finish can pile
    up an unbounded number of simultaneous runs entirely on their own,
    starving the shared execution slots (and the shared OnDemand-funded
    key, when that's in play) for everyone else. Exempt for the arena
    admin, same as the quota above — they're the one operating the arena,
    not competing with other users for the same slots."""
    if is_admin(user):
        return
    limit = max_active_tasks(user)
    active_count = len(
        db.runs.distinct(
            "task_id",
            {"submitted_by_user_id": user["_id"], "status": {"$in": ["pending", "running"]}, "is_deleted": {"$ne": True}},
        )
    )
    if active_count + task_count > limit:
        raise HTTPException(
            status_code=409,
            detail=f"Your active-task limit is {limit}. You have {active_count} active and requested {task_count}; wait for a task to complete before submitting more.",
        )


def record_submission(db: Database, user: dict, task_ids: list[str]) -> None:
    """Charge this user for the tasks they just submitted. Admins are
    unlimited, so nothing is recorded for them."""
    if is_admin(user) or not task_ids:
        return
    now = dt.datetime.now(dt.timezone.utc)
    db.task_submissions.insert_many(
        [
            {"_id": next_id(db, "task_submissions"), "user_id": user["_id"], "task_id": task_id, "created_at": now}
            for task_id in task_ids
        ]
    )
