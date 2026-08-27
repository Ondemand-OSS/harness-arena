"""Background execution of a submitted benchmark.

A submission returns immediately with a Batch id; the tasks then run in the
background, one task at a time (each task still runs its harnesses
concurrently). That ordering is deliberate: it means a finished task's
deliverables are available for grading right away, while the rest of the
batch is still working — rather than the whole submission being opaque
until every task is done.

Progress lives in the `batches` collection rather than in memory, so the UI
can poll it and a page reload doesn't lose track of a run in flight.
"""
from __future__ import annotations

import asyncio
import datetime as dt

from pymongo.database import Database

from .db import get_client, next_id
from .mongo import MONGODB_DB_NAME
from .runner import hold_lease, new_lease_fields, run_task, stale_lease_filter


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def _execute_batch(batch_id: int) -> None:
    db = get_client()[MONGODB_DB_NAME]
    batch = db.batches.find_one({"_id": batch_id})
    if batch is None:
        return
    async with hold_lease(db, "batches", batch_id):
        await _execute_batch_leased(db, batch_id, batch)


async def _execute_batch_leased(db, batch_id: int, batch: dict) -> None:
    task_ids = batch["task_ids"]
    harness_keys = batch["harness_keys"]
    provider_config_id = batch.get("provider_config_id")
    ondemand_model_id = batch.get("ondemand_model_id")
    # Carried onto every run so a failed one is retryable by the person who
    # submitted the batch, exactly like a directly-triggered run.
    user_id = batch.get("user_id")
    completed: list[str] = []
    error_message = ""

    for task_id in task_ids:
        db.batches.update_one({"_id": batch_id}, {"$set": {"current_task_id": task_id}})
        try:
            await run_task(
                db,
                task_id,
                harness_keys,
                force=False,
                provider_config_id=provider_config_id,
                user_id=user_id,
                ondemand_model_id=ondemand_model_id,
            )
        except Exception as exc:
            # One bad task shouldn't abandon the rest of the batch; record
            # it and keep going.
            error_message = f"{task_id}: {exc}"
        completed.append(task_id)
        db.batches.update_one({"_id": batch_id}, {"$set": {"completed_task_ids": completed}})

    db.batches.update_one(
        {"_id": batch_id},
        {
            "$set": {
                "current_task_id": "",
                "status": "error" if error_message else "done",
                "error_message": error_message,
                "finished_at": _utcnow(),
            }
        },
    )


def reconcile_orphaned_batches(db: Database) -> int:
    """Fix up any batch left `status: "running"` by a previous process.

    Mirrors runner.reconcile_orphaned_runs's reasoning, for the same
    architectural gap in the `batches` collection: a batch only advances
    via the in-process asyncio task `start_batch` creates (see _RUNNING
    below), with no persistent worker/queue behind it. If the process
    restarts (deploy, crash) while a batch is mid-loop, that task is gone
    for good — the batch document is left stuck at status "running"
    forever, with nothing left to ever finish or fail it.

    That stuck state isn't just cosmetic: routers/tasks.py's
    `_ensure_not_running` refuses to delete a task's own (already-failed)
    runs while ANY batch's `current_task_id` still names it, so an
    orphaned batch permanently blocks deleting the task it was working on
    when the process died.

    Like its runs counterpart, "orphaned" is decided by the batch's own
    execution lease (runner.hold_lease) rather than by this process's age —
    otherwise a replica starting up alongside a live one would abort that
    peer's in-progress batch.
    """
    now = _utcnow()
    result = db.batches.update_many(
        {"status": "running", **stale_lease_filter(now)},
        {
            "$set": {
                "status": "error",
                "current_task_id": "",
                # NOTE: unlike a run's error_message (masked for non-admins
                # by routers/runs.py's _public_error_message), a batch's
                # error_message has no such masking today — BatchOut exposes
                # it as-is to whoever can see the batch, which per
                # routers/batches.py is any signed-in user, not just the
                # admin. Worth the same treatment before this is relied on.
                "error_message": "Lost its executor while running — the process driving it stopped "
                "renewing its lease, so it never finished.",
                "finished_at": now,
            }
        },
    )
    return result.modified_count


def start_batch(
    db: Database,
    task_ids: list[str],
    harness_keys: list[str],
    user_id: int | None,
    provider_config_id: int | None = None,
    ondemand_model_id: int | None = None,
) -> dict:
    """Creates the batch document and kicks off background execution."""
    batch_id = next_id(db, "batches")
    doc = {
        "_id": batch_id,
        "user_id": user_id,
        "task_ids": task_ids,
        "harness_keys": harness_keys,
        "provider_config_id": provider_config_id,
        "ondemand_model_id": ondemand_model_id,
        "status": "running",
        "completed_task_ids": [],
        "current_task_id": "",
        "error_message": "",
        "created_at": _utcnow(),
        "finished_at": None,
        # Leased to this process from the moment the row exists — see
        # runner.new_lease_fields.
        **new_lease_fields(),
    }
    db.batches.insert_one(doc)
    # Fire-and-forget on the running event loop. Held in a module-level set
    # so it isn't garbage-collected mid-flight (asyncio only keeps weak
    # references to tasks).
    task = asyncio.create_task(_execute_batch(batch_id))
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return doc


_RUNNING: set[asyncio.Task] = set()
