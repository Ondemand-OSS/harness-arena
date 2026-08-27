from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from pymongo.database import Database

from ..batches import start_batch
from ..db import get_db
from ..logger import log_activity
from ..public_errors import rate_limit_message
from ..harnesses.registry import enabled_harness_keys
from ..rate_limit import record_submission, require_no_active_runs, require_quota
from ..runner import latest_runs_by_harness
from ..users import current_user, is_admin, require_user
from .runs import MIN_HARNESSES_PER_BATTLE, require_enabled_profile, require_ondemand_selection, require_reference_files_attached

router = APIRouter(prefix="/api/batches", tags=["batches"])


class SubmitIn(BaseModel):
    task_ids: list[str]
    harness_keys: list[str] | None = None
    provider_config_id: int | None = None
    # Deprecated — retained only for API compatibility with older clients.
    # See schemas.RunRequest.ondemand_model_id: the value is resolved
    # server-side from the shared free profile's admin-set mapping instead.
    ondemand_model_id: int | None = None


class BatchTaskOut(BaseModel):
    task_id: str
    title: str
    state: str  # queued | running | ready | judged
    done_outputs: int


class BatchOut(BaseModel):
    id: int
    status: str
    submitted_by: str | None
    total: int
    completed: int
    current_task_id: str
    error_message: str
    provider_config_id: int | None = None
    tasks: list[BatchTaskOut]


def _public_batch_error_message(batch: dict, viewer: dict | None) -> str:
    """Mirrors routers/runs.py's `_public_error_message`: the real
    `error_message` can be a raw adapter/provider error string, an internal
    exception (see runner.py's crash handling and the reconciler's stale-
    lease message) — same category of detail that stays admin-only for a
    run. Only the arena admin sees it here either; a batch's own submitter
    gets the same generic message as anyone else, same as a run's submitter
    already does."""
    if batch.get("status") != "error" or is_admin(viewer):
        return batch.get("error_message", "")
    if message := rate_limit_message(batch.get("error_message", "")):
        return message
    return "Batch failed."


def _batch_out(db: Database, batch: dict, viewer: dict | None = None) -> BatchOut:
    user_id = viewer["_id"] if viewer else None
    task_ids = batch["task_ids"]
    completed = set(batch.get("completed_task_ids", []))
    titles = (
        {t["_id"]: t.get("title", "") for t in db.tasks.find({"_id": {"$in": task_ids}}, {"title": 1})}
        if task_ids
        else {}
    )
    # Scoped to THIS batch's own profile — a score is stored per
    # (task_id, provider_config_id) (see scores' unique index), so a task
    # the user judged under an EARLIER, different profile must not read as
    # "already judged" here: Regenerate no longer dedupes runs (see
    # runner.run_task), so this exact batch can be a genuinely fresh,
    # unjudged round for a task that was judged once before under a
    # different model. Without this scope, a task the user judged months
    # ago showed "Judged by you" the instant a brand new battle for it was
    # even submitted, before a single harness had finished.
    #
    # `is_deleted` also has to be excluded here — an admin's "Delete
    # results" (routers/tasks.py's _delete_results) soft-deletes the score
    # row rather than removing it, and every OTHER score query in this app
    # already filters that out (see routers/scores.py, routers/runs.py,
    # elo.py, leaderboard.py). Missing it here meant a task whose results
    # were fully deleted still showed "Judged by you" forever after,
    # because the now-archived score document still existed to match on.
    judged = (
        set(
            db.scores.distinct(
                "task_id",
                {
                    "user_id": user_id,
                    "provider_config_id": batch.get("provider_config_id"),
                    "is_deleted": {"$ne": True},
                },
            )
        )
        if user_id is not None
        else set()
    )

    tasks = []
    for tid in task_ids:
        done_outputs = sum(1 for r in latest_runs_by_harness(db, tid).values() if r["status"] == "done")
        if tid in judged:
            state = "judged"
        elif tid in completed:
            state = "ready"
        elif tid == batch.get("current_task_id"):
            state = "running"
        else:
            state = "queued"
        tasks.append(BatchTaskOut(task_id=tid, title=titles.get(tid, tid), state=state, done_outputs=done_outputs))

    submitted_by = None
    if batch.get("user_id") is not None:
        user = db.users.find_one({"_id": batch["user_id"]})
        if user is not None:
            submitted_by = user.get("display_name") or user.get("username")

    return BatchOut(
        id=batch["_id"],
        status=batch["status"],
        submitted_by=submitted_by,
        total=len(task_ids),
        completed=len(completed),
        current_task_id=batch.get("current_task_id", ""),
        error_message=_public_batch_error_message(batch, viewer),
        provider_config_id=batch.get("provider_config_id"),
        tasks=tasks,
    )


@router.post("", response_model=BatchOut)
async def submit_batch(
    body: SubmitIn,
    db: Database = Depends(get_db),
    user: dict = Depends(require_user),
):
    # Must be async: start_batch() calls asyncio.create_task(), which needs
    # the running event loop. A sync def here would run in FastAPI's worker
    # thread pool instead, where there is no running loop to schedule onto
    # (hit this directly: "RuntimeError: no running event loop").
    """Queue tasks for evaluation. Returns immediately — the run proceeds in
    the background so finished tasks can be graded while the rest work."""
    if not body.task_ids:
        raise HTTPException(status_code=400, detail="select at least one task")
    if len(body.task_ids) > 1:
        raise HTTPException(status_code=400, detail="only 1 task can be run at a time")

    known = {t["_id"] for t in db.tasks.find({"_id": {"$in": body.task_ids}}, {"_id": 1})}
    unknown = [t for t in body.task_ids if t not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown task(s): {unknown}")

    enabled = enabled_harness_keys(db)
    harness_keys = body.harness_keys or enabled
    not_runnable = [k for k in harness_keys if k not in enabled]
    if not_runnable:
        raise HTTPException(status_code=400, detail=f"harness(es) not runnable: {not_runnable}")
    if len(harness_keys) < MIN_HARNESSES_PER_BATTLE:
        raise HTTPException(
            status_code=400,
            detail=f"pick at least {MIN_HARNESSES_PER_BATTLE} harnesses. A single harness has nothing to compare against",
        )
    require_enabled_profile(db, user, body.provider_config_id)
    resolved_ondemand_model_id = require_ondemand_selection(db, user, harness_keys, body.provider_config_id)
    require_reference_files_attached(db, body.task_ids)

    require_quota(db, user, task_count=len(body.task_ids))
    require_no_active_runs(db, user, task_count=len(body.task_ids))
    batch = start_batch(db, body.task_ids, harness_keys, user["_id"], body.provider_config_id, resolved_ondemand_model_id)
    record_submission(db, user, body.task_ids)
    log_activity(
        db,
        action="BATCH_SUBMIT",
        user_id=user["_id"],
        message=f"queued benchmark batch {batch['_id']} over {len(body.task_ids)} task(s)",
        metadata={
            "batch_id": batch["_id"],
            "task_ids": body.task_ids,
            "harness_keys": harness_keys,
            "provider_config_id": body.provider_config_id,
        },
        route="/api/batches",
    )
    return _batch_out(db, batch, user)


@router.get("", response_model=list[BatchOut])
def list_batches(active_only: bool = False, db: Database = Depends(get_db), user: dict | None = Depends(current_user)):
    query = {"status": "running"} if active_only else {}
    batches = db.batches.find(query).sort("_id", -1).limit(20)
    return [_batch_out(db, b, user) for b in batches]


@router.get("/{batch_id}", response_model=BatchOut)
def get_batch(batch_id: int, db: Database = Depends(get_db), user: dict | None = Depends(current_user)):
    batch = db.batches.find_one({"_id": batch_id})
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return _batch_out(db, batch, user)
