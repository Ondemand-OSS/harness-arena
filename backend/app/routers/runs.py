from __future__ import annotations

import datetime as dt
import json
import os
import asyncio
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pymongo.database import Database

from ..cache import get_json as cache_get, invalidate as cache_invalidate, mark_response as cache_mark, set_json as cache_set
from ..db import get_db
from ..logger import log_activity
from ..public_errors import rate_limit_message
from ..harnesses.registry import all_adapters, enabled_harness_keys
from ..preview import build_preview, render_pptx_as_pdf
from ..rate_limit import record_submission, require_no_active_runs, require_quota
from ..runner import (
    all_runs_for_task,
    latest_runs_by_harness,
    new_lease_fields,
    resolve_ondemand_model_id,
    retry_existing_run,
    start_runs,
)
from ..schemas import BoardRowOut, BoardTaskOut, CompareOut, DeliverableOut, RunOut, RunRequest, RunsBoardOut, RunsOverviewIn, RunTriggerOut, TaskOut, TaskOverviewOut
from ..taxonomy import parse_reference_filenames
from ..users import current_user, is_admin, require_arena_admin, require_user
from ..webproject import is_web_project
from .scores import build_compare_entries, package_json_bytes_by_run, website_and_extra_ids
from .tasks import MAX_TASKS_PAGE_LIMIT, list_tasks as _list_tasks_for_board

router = APIRouter(prefix="/api/runs", tags=["runs"])

# Distinguishes "the caller didn't pass submitted_by at all" (look it up)
# from "the caller looked it up and there wasn't one" (None is a real,
# already-known answer) in _run_out below.
_UNSET = object()

# The imported arena results predate provider profiles. The benchmark owner
# confirmed those recorded runs all used this model, so expose the factual
# legacy value rather than an empty “model” cell in the UI. New runs always
# carry their selected model from runner.py.
LEGACY_RECORDED_MODEL = "DeepSeek V4 Flash"

# One harness alone isn't a comparison — the whole premise is judging the
# same model's output across different harnesses, so a battle needs at
# least two. Enforced server-side as well as in the UI.
MIN_HARNESSES_PER_BATTLE = 2


def require_enabled_profile(db: Database, user: dict, provider_config_id: int | None) -> None:
    """A disabled free profile (routers/config.py's enabled toggle) is
    already hidden from a non-admin's model picker, but that's a UI
    filter, not enforcement — this closes the gap for a request that names
    a disabled profile's id directly. The admin is exempt: turning a
    profile off is their own call, not something that should also lock
    them out of using it. No-op when no profile was selected at all."""
    if provider_config_id is None or is_admin(user):
        return
    doc = db.provider_config.find_one({"_id": provider_config_id}, {"enabled": 1})
    if doc is not None and doc.get("enabled", True) is False:
        raise HTTPException(status_code=400, detail="This model is currently disabled. Pick another one.")


def require_ondemand_selection(
    db: Database, user: dict, harness_keys: list[str], provider_config_id: int | None
) -> int | None:
    """OnDemand doesn't fit the shared-profile model every other harness
    uses (see harnesses/base.py's ProviderSettings note) — when it's part
    of a battle, the OnDemand model to run is no longer picked by hand; it's
    resolved from the admin-set mapping on the shared free profile
    (provider_config.ondemand_model_id, see routers/config.py and
    runner.resolve_ondemand_model_id), so this just checks that the mapping
    actually exists and the user has their own OnDemand key. Called from
    both trigger_run below and batches.py's submit_batch, before run_task
    ever runs — same placement as the MIN_HARNESSES_PER_BATTLE check.
    Returns the resolved ondemand_model_id (None if OnDemand isn't part of
    this battle) for the caller to pass on to start_runs/start_batch."""
    if "ondemand" not in harness_keys:
        return None
    ondemand_model_id = resolve_ondemand_model_id(db, provider_config_id)
    if ondemand_model_id is None:
        raise HTTPException(
            status_code=400,
            detail="OnDemand isn't set up for the selected model yet — ask the arena admin to map it to an OnDemand model.",
        )
    model_doc = db.ondemand_models.find_one({"_id": ondemand_model_id})
    if model_doc is None:
        raise HTTPException(status_code=404, detail="OnDemand model not found.")
    if model_doc.get("enabled", True) is False:
        raise HTTPException(status_code=400, detail="This OnDemand model is currently disabled. Pick another model.")
    if not user.get("ondemand_api_key"):
        raise HTTPException(
            status_code=400,
            detail="Set your OnDemand API key in Setup before running OnDemand.",
        )
    return ondemand_model_id


def require_reference_files_attached(db: Database, task_ids: list[str]) -> None:
    """A task's `reference_files` text names material a run needs to
    actually read — without this check, naming a file that was never
    uploaded (see routers/tasks.py's reference-files endpoints) silently
    degrades to a run that either hallucinates the content or never sees
    it at all (see harnesses/_prompt.py / ondemand.py's session upload),
    with no signal until a judge notices later. Refuse to start the battle
    at all instead, for every task in the request — same placement as
    require_ondemand_selection, before run_task/start_batch ever runs."""
    problems = []
    for task in db.tasks.find({"_id": {"$in": task_ids}}, {"_id": 1, "reference_files": 1}):
        expected = parse_reference_filenames(task.get("reference_files", ""))
        if not expected:
            continue
        attached = {
            d["filename"] for d in db.task_reference_files.find({"task_id": task["_id"]}, {"filename": 1})
        }
        missing = [name for name in expected if name not in attached]
        if missing:
            problems.append(f"{task['_id']} (missing: {', '.join(missing)})")
    if problems:
        raise HTTPException(
            status_code=400,
            detail="Upload the reference file(s) these tasks name before running them: " + "; ".join(problems),
        )


def _may_retry(run: dict, viewer: dict | None) -> bool:
    """A failed run can be retried by whoever submitted it, or by the admin.
    Runs created before submitter tracking existed have no owner, so only
    the admin can retry those."""
    if viewer is None or run.get("status") != "error":
        return False
    return is_admin(viewer) or run.get("submitted_by_user_id") == viewer["_id"]


def _ondemand_session_ids(run: dict) -> list[str]:
    values = [run.get("ondemand_session_id"), *(run.get("ondemand_session_ids") or [])]
    return list(dict.fromkeys(session_id for session_id in values if isinstance(session_id, str) and session_id))


def _public_error_message(run: dict, viewer: dict | None) -> str:
    """The real error_message can be a raw adapter/provider error string —
    stderr excerpts, a provider's own error body, internal exception text
    (see harnesses/*.py's various `error_message=...`) — same category of
    detail `raw_log` is already admin-only for. Only a genuine `error`
    status needs masking: `error_message` is also reused for informational,
    already-user-facing text on other statuses (e.g. "Stopped by arena
    admin." — see runner.py's mark_stopped), which stay visible to
    everyone since nothing sensitive was ever put there."""
    if run.get("status") != "error" or is_admin(viewer):
        return run.get("error_message", "")
    if message := rate_limit_message(run.get("error_message", "")):
        return message
    return "Run failed."


def _display_model(run: dict, db: Database, ondemand_labels: dict[int, str] | None = None) -> str:
    """Never expose OnDemand's technical endpoint id in arena UI data."""
    if run.get("harness_key") == "ondemand" and run.get("ondemand_model_id") is not None:
        model_id = run["ondemand_model_id"]
        if ondemand_labels is not None:
            label = ondemand_labels.get(model_id)
        else:
            doc = db.ondemand_models.find_one({"_id": model_id}, {"label": 1})
            label = (doc or {}).get("label")
        if label:
            return label
    return run.get("model") or (LEGACY_RECORDED_MODEL if run.get("status") == "done" else "")


def _run_out(
    run: dict,
    db: Database,
    viewer: dict | None = None,
    *,
    deliverables: list[DeliverableOut] | None = None,
    submitted_by: str | None | object = _UNSET,
    already_scored: float | None = None,
    community_avg_score: float | None = None,
    community_vote_count: int = 0,
    ondemand_labels: dict[int, str] | None = None,
) -> RunOut:
    """`deliverables`/`submitted_by` let a caller that already bulk-fetched
    this data (see runs_overview below) skip the two per-run Mongo queries
    below — passing either explicitly (even `None` for `submitted_by`, to
    mean "looked it up, there wasn't one") short-circuits that lookup.
    Every existing single-run call site leaves both unset and gets the
    original per-call-query behavior, unchanged.

    `already_scored`/`community_avg_score`/`community_vote_count` have no
    per-run fallback lookup at all — only runs_overview passes them
    (it already has every score for the task loaded), since computing them
    here would mean a scores query per run for every other caller, none of
    which currently need it."""
    if deliverables is None:
        deliverables = [
            DeliverableOut(id=d["_id"], filename=d["filename"], media_type=d["media_type"], size_bytes=d["size_bytes"], relpath=d.get("relpath", ""))
            for d in db.deliverables.find({"run_id": run["_id"]}, {"content": 0})
        ]
    if submitted_by is _UNSET:
        submitted_by = None
        if run.get("submitted_by_user_id") is not None:
            submitter = db.users.find_one({"_id": run["submitted_by_user_id"]})
            if submitter is not None:
                submitted_by = submitter.get("display_name") or submitter.get("username")
    return RunOut(
        id=run["_id"],
        round_id=run.get("round_id"),
        task_id=run["task_id"],
        harness_key=run["harness_key"],
        provider_config_id=run.get("provider_config_id"),
        model=_display_model(run, db, ondemand_labels),
        status=run["status"],
        started_at=run.get("started_at"),
        finished_at=run.get("finished_at"),
        error_message=_public_error_message(run, viewer),
        deliverables_done=run.get("deliverables_done", len(deliverables)),
        deliverables_expected=run.get("deliverables_expected", len(deliverables)),
        deliverables=deliverables,
        submitted_by=submitted_by,
        can_retry=_may_retry(run, viewer),
        can_stop=bool(viewer and is_admin(viewer) and run.get("status") in {"pending", "running"}),
        ondemand_session_id=run.get("ondemand_session_id") if is_admin(viewer) else None,
        ondemand_session_ids=_ondemand_session_ids(run) if is_admin(viewer) else None,
        is_retrying=bool(run.get("is_retrying")),
        raw_log=run.get("raw_log", "") if is_admin(viewer) else None,
        already_scored=already_scored,
        community_avg_score=community_avg_score,
        community_vote_count=community_vote_count,
    )


@router.post("", response_model=RunTriggerOut)
async def trigger_run(body: RunRequest, db: Database = Depends(get_db), user: dict = Depends(require_user)):
    task = db.tasks.find_one({"_id": body.task_id})
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    enabled = enabled_harness_keys(db)
    harness_keys = body.harness_keys or enabled
    unknown = [k for k in harness_keys if k not in enabled]
    if unknown:
        raise HTTPException(status_code=400, detail=f"harness(es) not runnable: {unknown}")
    if len(harness_keys) < MIN_HARNESSES_PER_BATTLE:
        raise HTTPException(
            status_code=400,
            detail=f"pick at least {MIN_HARNESSES_PER_BATTLE} harnesses. A single harness has nothing to compare against",
        )
    require_enabled_profile(db, user, body.provider_config_id)
    resolved_ondemand_model_id = require_ondemand_selection(db, user, harness_keys, body.provider_config_id)
    require_reference_files_attached(db, [body.task_id])

    require_quota(db, user, task_count=1)
    require_no_active_runs(db, user)
    # Create records immediately; harness execution continues in the background.
    run_ids, reused_same_model = start_runs(
        db,
        body.task_id,
        harness_keys,
        force=body.force,
        provider_config_id=body.provider_config_id,
        user_id=user["_id"],
        ondemand_model_id=resolved_ondemand_model_id,
        # Charge the quota only once the battle has actually run, same as
        # the old await-then-charge behavior — not just for having been
        # queued. `db`/`user` are plain Mongo handles/dicts (nothing
        # request-scoped — see db.py), so closing over them here and using
        # them after this request has already returned is safe.
        on_complete=lambda: record_submission(db, user, [body.task_id]),
    )
    runs = list(db.runs.find({"_id": {"$in": run_ids}}))
    log_activity(
        db,
        action="RUN_SUBMIT",
        user_id=user["_id"],
        message=f"submitted a battle for task {body.task_id} across {len(harness_keys)} harness(es)",
        metadata={
            "task_id": body.task_id,
            "harness_keys": harness_keys,
            "run_ids": run_ids,
            "round_id": runs[0].get("round_id") if runs else None,
            "provider_config_id": body.provider_config_id,
            "forced": bool(body.force),
        },
        route="/api/runs",
    )
    return RunTriggerOut(
        runs=[_run_out(r, db, user) for r in runs], reused_same_model=reused_same_model
    )


@router.post("/{run_id}/retry", response_model=RunOut)
async def retry_run(run_id: int, db: Database = Depends(get_db), user: dict = Depends(require_user)):
    """Re-run a single failed harness. Deliberately does NOT consume the
    submission quota (see rate_limit.py): an errored run produced nothing
    usable, so charging for it would penalize the user for a failure that
    wasn't theirs."""
    run = db.runs.find_one({"_id": run_id})
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.get("status") != "error":
        raise HTTPException(status_code=400, detail="only a failed run can be retried")
    if not _may_retry(run, user):
        raise HTTPException(status_code=403, detail="only the person who submitted this run (or the admin) can retry it")

    # A retry is the same battle attempt recovering from a harness failure,
    # not a new comparison. Reuse its id so Battle Log continues to update
    # one row and replaces any failed-attempt files atomically on success.
    db.deliverables.delete_many({"run_id": run_id})
    db.runs.update_one(
        {"_id": run_id},
        {
            "$set": {
                "status": "pending",
                "started_at": dt.datetime.now(dt.timezone.utc),
                "finished_at": None,
                "error_message": "",
                # Preserve the failed attempt's reason for the admin audit
                # trail while the retried attempt gets its own clean status.
                "previous_error_message": run.get("error_message", ""),
                "raw_log": "",
                "ondemand_session_id": "",
                "deliverables_done": 0,
                "deliverables_expected": 0,
                "is_retrying": True,
                "stop_requested": False,
                # Lease this row to this process as part of the same write
                # that puts it back to `pending`. Without it the row would
                # briefly carry the FAILED attempt's long-stale heartbeat
                # while already pending again, and a concurrent
                # reconciliation sweep could immediately re-fail the retry
                # before it ever started. See runner.new_lease_fields.
                **new_lease_fields(),
            },
            "$inc": {"retry_count": 1},
        },
    )
    cache_invalidate("runs_board")
    fresh = db.runs.find_one({"_id": run_id})
    log_activity(
        db,
        action="RUN_RETRY",
        user_id=user["_id"],
        message=f"retried failed run {run_id} ({run.get('harness_key')})",
        metadata={
            "run_id": run_id,
            "task_id": run.get("task_id"),
            "harness_key": run.get("harness_key"),
            "round_id": run.get("round_id"),
            "retry_count": (run.get("retry_count") or 0) + 1,
        },
        route="/api/runs/{run_id}/retry",
    )
    asyncio.create_task(retry_existing_run(run_id, user_id=user["_id"]))
    return _run_out(fresh, db, user)


@router.post("/{run_id}/stop", response_model=RunOut)
def stop_run(run_id: int, db: Database = Depends(get_db), admin: dict = Depends(require_arena_admin)):
    """Ask the worker to cancel a pending or active harness run."""
    run = db.runs.find_one({"_id": run_id})
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.get("status") not in {"pending", "running"}:
        raise HTTPException(status_code=400, detail="only pending or running runs can be stopped")
    db.runs.update_one({"_id": run_id}, {"$set": {"stop_requested": True}})
    log_activity(
        db,
        action="RUN_STOP",
        user_id=admin["_id"],
        message=f"requested stop of run {run_id} ({run.get('harness_key')})",
        metadata={"run_id": run_id, "task_id": run.get("task_id"), "harness_key": run.get("harness_key"), "previous_status": run.get("status")},
        route="/api/runs/{run_id}/stop",
    )
    return _run_out(db.runs.find_one({"_id": run_id}), db, admin)


@router.delete("/round/{round_id}", status_code=204)
def delete_round(round_id: str, db: Database = Depends(get_db), admin: dict = Depends(require_arena_admin)):
    """Archive every run and deliverable from one completed battle round."""
    round_values: list[str | int] = [round_id]
    if round_id.isdigit():
        round_values.append(int(round_id))
    runs = list(db.runs.find({"round_id": {"$in": round_values}, "is_deleted": {"$ne": True}}))
    if not runs:
        raise HTTPException(status_code=404, detail="round not found")
    if any(run.get("status") in {"pending", "running"} for run in runs):
        raise HTTPException(status_code=400, detail="cannot delete a round while it has active runs")

    archived_at = dt.datetime.now(dt.timezone.utc)
    run_ids = [run["_id"] for run in runs]
    db.runs.update_many(
        {"_id": {"$in": run_ids}, "is_deleted": {"$ne": True}},
        {"$set": {"is_deleted": True, "deleted_at": archived_at}},
    )
    db.deliverables.update_many(
        {"run_id": {"$in": run_ids}},
        {"$set": {"is_deleted": True, "deleted_at": archived_at}},
    )
    cache_invalidate("runs_board", "stats", "leaderboard")
    log_activity(
        db,
        action="ROUND_DELETE",
        user_id=admin["_id"],
        message=f"archived battle round {round_id} ({len(run_ids)} runs)",
        metadata={"round_id": round_id, "run_ids": run_ids, "task_ids": sorted({run["task_id"] for run in runs})},
        route="/api/runs/round/{round_id}",
    )


@router.delete("/{run_id}", status_code=204)
def delete_failed_run(run_id: int, db: Database = Depends(get_db), admin: dict = Depends(require_arena_admin)):
    """Archive one failed attempt without touching any other harness result.

    Failed runs can have a partial workspace/deliverable record, so those
    are archived too. The normal run queries already omit ``is_deleted``
    records, which also prevents a deleted failure becoming a retry target.
    """
    run = db.runs.find_one({"_id": run_id})
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.get("status") != "error":
        raise HTTPException(status_code=400, detail="only a failed run can be deleted individually")
    archived_at = dt.datetime.now(dt.timezone.utc)
    db.runs.update_one({"_id": run_id}, {"$set": {"is_deleted": True, "deleted_at": archived_at}})
    db.deliverables.update_many({"run_id": run_id}, {"$set": {"is_deleted": True, "deleted_at": archived_at}})
    cache_invalidate("runs_board", "stats", "leaderboard")
    log_activity(
        db,
        action="RUN_DELETE",
        user_id=admin["_id"],
        message=f"archived failed run {run_id} ({run.get('harness_key')})",
        metadata={"run_id": run_id, "task_id": run.get("task_id"), "harness_key": run.get("harness_key"), "round_id": run.get("round_id")},
        route="/api/runs/{run_id}",
    )


class AdminRunSummary(BaseModel):
    """One row in the admin-only cross-task run monitor — deliberately
    leaner than RunOut (no per-run deliverables lookup) since this can list
    hundreds of runs at once; see admin_list_runs below."""

    id: int
    # Shared UUID of the battle trigger that created this harness run.
    # The integer alternative covers historical rows from before UUID rounds.
    round_id: str | int | None = None
    task_id: str
    task_title: str
    harness_key: str
    model: str
    status: str
    provider_config_id: int | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    error_message: str = ""
    previous_error_message: str = ""
    submitted_by: str | None = None
    deliverables_done: int = 0
    deliverables_expected: int = 0
    raw_log: str = ""
    ondemand_session_id: str | None = None
    ondemand_session_ids: list[str] = []
    # How many times this exact run row has been retried in place (see
    # routers/runs.py's retry_run, which $inc's this instead of creating a
    # new row) — 0 for a run that's whatever it is on the first attempt.
    # Surfaced so a run that's now "done" doesn't read as indistinguishable
    # from one that succeeded on the first try when it actually needed a
    # retry to get there.
    retry_count: int = 0


@router.get("/admin/overview", response_model=list[AdminRunSummary])
def admin_list_runs(
    status: str | None = None,
    limit: int = 100,
    db: Database = Depends(get_db),
    user: dict = Depends(require_user),
):
    """Every run across the WHOLE arena, not scoped to one task — lets the
    admin see what's currently pending/running, what just finished, and
    what just failed, with a truncated log excerpt for each, without
    needing host or database access. `status` narrows to one of
    pending/running/done/error; omit it to see everything, newest first."""
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="admin only")

    query: dict = {}
    if status is not None:
        query["status"] = status
    limit = max(1, min(limit, 500))
    runs = list(db.runs.find(query).sort("_id", -1).limit(limit))

    task_ids = list({r["task_id"] for r in runs})
    titles = {t["_id"]: t.get("title", "") for t in db.tasks.find({"_id": {"$in": task_ids}}, {"title": 1})}
    submitter_ids = list({r["submitted_by_user_id"] for r in runs if r.get("submitted_by_user_id") is not None})
    submitters = {
        u["_id"]: (u.get("display_name") or u.get("username"))
        for u in db.users.find({"_id": {"$in": submitter_ids}})
    }
    ondemand_model_ids = {r.get("ondemand_model_id") for r in runs if r.get("harness_key") == "ondemand" and r.get("ondemand_model_id") is not None}
    ondemand_labels = {
        doc["_id"]: doc.get("label", "")
        for doc in db.ondemand_models.find({"_id": {"$in": list(ondemand_model_ids)}}, {"label": 1})
    }

    return [
        AdminRunSummary(
            id=r["_id"],
            round_id=r.get("round_id"),
            task_id=r["task_id"],
            task_title=titles.get(r["task_id"], r["task_id"]),
            harness_key=r["harness_key"],
            model=_display_model(r, db, ondemand_labels),
            status=r["status"],
            provider_config_id=r.get("provider_config_id"),
            started_at=r.get("started_at"),
            finished_at=r.get("finished_at"),
            error_message=r.get("error_message", ""),
            previous_error_message=r.get("previous_error_message", ""),
            submitted_by=submitters.get(r.get("submitted_by_user_id")),
            deliverables_done=r.get("deliverables_done", 0),
            deliverables_expected=r.get("deliverables_expected", 0),
            raw_log=r.get("raw_log", ""),
            ondemand_session_id=r.get("ondemand_session_id"),
            ondemand_session_ids=_ondemand_session_ids(r),
            # Older in-place retries predate retry_count. A preserved
            # previous failure still proves this row was retried once.
            retry_count=max(r.get("retry_count", 0), 1 if r.get("previous_error_message") else 0),
        )
        for r in runs
    ]


@router.get("/by-task/{task_id}", response_model=list[RunOut])
def list_runs_for_task(
    task_id: str,
    provider_config_id: int | None = None,
    db: Database = Depends(get_db),
    user: dict | None = Depends(current_user),
):
    """The *current* run per harness for this task (i.e. what Regenerate
    would replace) — not full history. A Regenerate leaves the old run
    document in place for audit purposes, but it should never reappear
    here once a newer one exists for the same harness."""
    current = latest_runs_by_harness(db, task_id, provider_config_id=provider_config_id)
    return [_run_out(r, db, user) for r in sorted(current.values(), key=lambda r: r["_id"])]


@router.get("/by-task/{task_id}/history", response_model=list[RunOut])
def list_run_history_for_task(
    task_id: str,
    provider_config_id: int | None = None,
    db: Database = Depends(get_db),
    user: dict | None = Depends(current_user),
):
    """Every run ever created for this task — every Regenerate, every model
    tried, not just the current one per harness (see `/by-task/{task_id}`
    for that). Battle Log uses this to show full history; Evaluate
    deliberately keeps using the current-only endpoint since it's showing
    "what's gradeable right now," not an audit trail."""
    runs = all_runs_for_task(db, task_id, provider_config_id=provider_config_id)
    return [_run_out(r, db, user) for r in runs]


def _resolve_ever_done_provider_config_id(current_by_harness_asc: list[dict], history_desc: list[dict]) -> int | None:
    """Which provider_config_id "the" comparison for this task is under,
    when no explicit profile was requested.

    Candidates: each harness's own best-available done run — its CURRENT
    run if that's done, else its most recent historical done run (a
    harness whose current attempt errored or is still running doesn't
    thereby lose a real prior success). Among those candidates, picks the
    one with the HIGHEST run id — i.e. whichever harness was done most
    recently wins, and the whole comparison is built around ITS profile.

    Recency, not "first found", has to be the tiebreaker: with runs no
    longer deduped (see runner.run_task), a task can have several
    completed profile groups sitting around from different points in
    time. Picking anything other than the most recent one means a fresh
    battle you just ran (say, Claude Code + Codex only, deliberately
    without OnDemand) can get silently overridden by a stale, larger,
    older group that happens to include a harness you didn't even select
    this time (OnDemand, still sitting on a run from days ago) — exactly
    the bug this replaced: an old 3-harness comparison kept resurfacing
    over a newer, genuinely-current 2-harness one, and the new run's own
    harnesses had nowhere to show up at all, neither as the live
    comparison nor as a past attempt (see BattleLog.jsx's pastAttempts,
    which also treats "current" as taken)."""
    candidates: dict[str, dict] = {}
    for run in current_by_harness_asc:
        if run["status"] == "done":
            candidates[run["harness_key"]] = run
    for run in history_desc:
        if run["status"] == "done" and run["harness_key"] not in candidates:
            candidates[run["harness_key"]] = run
    if not candidates:
        return None
    return max(candidates.values(), key=lambda r: r["_id"]).get("provider_config_id")


@router.post("/overview", response_model=list[TaskOverviewOut])
def runs_overview(
    body: RunsOverviewIn = Body(...), db: Database = Depends(get_db), user: dict | None = Depends(current_user)
):
    """Return runs, history, and comparison data for multiple tasks.

    Produces byte-identical results to calling those three endpoints per
    task: reuses the exact same `_run_out`/`build_compare_entries` logic
    those endpoints use, just fed from bulk-fetched dicts instead of one
    Mongo query per run/task.
    """
    task_ids = list(dict.fromkeys(body.task_ids))  # de-dup, keep order
    return list(_build_overviews(task_ids, db, user).values())


def _build_overviews(
    task_ids: list[str], db: Database, user: dict | None, *, adapters: dict | None = None
) -> dict[str, TaskOverviewOut]:
    """Shared core of POST /api/runs/overview and GET /api/runs/board —
    everything below used to live directly in runs_overview; pulled out so
    the board endpoint can reuse it instead of re-running the same bulk
    queries under a second code path."""
    if not task_ids:
        return {}

    # One query for every run of every requested task, instead of one
    # query per task. Sorted once, up front, exactly like
    # latest_runs_by_harness/all_runs_for_task do per-task today.
    runs_by_task: dict[str, list[dict]] = defaultdict(list)
    all_run_ids: list[int] = []
    for run in db.runs.find({"task_id": {"$in": task_ids}, "is_deleted": {"$ne": True}}).sort("_id", -1):
        runs_by_task[run["task_id"]].append(run)
        all_run_ids.append(run["_id"])

    ondemand_ids = list(
        {
            run["ondemand_model_id"]
            for runs in runs_by_task.values()
            for run in runs
            if run.get("harness_key") == "ondemand" and run.get("ondemand_model_id") is not None
        }
    )
    ondemand_labels = (
        {doc["_id"]: doc.get("label") for doc in db.ondemand_models.find({"_id": {"$in": ondemand_ids}}, {"label": 1})}
        if ondemand_ids
        else {}
    )

    # One query for every deliverable of every one of those runs.
    deliverables_by_run: dict[int, list[DeliverableOut]] = defaultdict(list)
    for d in db.deliverables.find({"run_id": {"$in": all_run_ids}}, {"content": 0}):
        deliverables_by_run[d["run_id"]].append(
            DeliverableOut(id=d["_id"], filename=d["filename"], media_type=d["media_type"], size_bytes=d["size_bytes"], relpath=d.get("relpath", ""))
        )

    # One query for every requested task's expected_deliverables + one
    # batched query for every package.json among these runs' deliverables —
    # together enough for website_and_extra_ids below, without a query per
    # run (see package_json_bytes_by_run's docstring).
    expected_deliverables_by_task = {
        t["_id"]: t.get("expected_deliverables", "") for t in db.tasks.find({"_id": {"$in": task_ids}}, {"expected_deliverables": 1})
    }
    # Restricted to web-project tasks' own runs, not every run being
    # overviewed — this endpoint backs Battle Log/Evaluate/Benchmark's
    # normal page loads, almost none of which are web projects, so
    # querying package.json for all_run_ids here was adding a Mongo round
    # trip to every single page load regardless of whether it ever mattered.
    web_project_run_ids = [
        run["_id"]
        for task_id, task_runs in runs_by_task.items()
        if is_web_project(expected_deliverables_by_task.get(task_id, ""))
        for run in task_runs
    ]
    package_json_by_run = package_json_bytes_by_run(web_project_run_ids, db)

    # One query for every submitter's display name across every run — the
    # ids come straight from the runs already fetched above, no need to
    # query db.runs a second time just for a field it already has.
    submitter_ids = {
        run["submitted_by_user_id"] for runs in runs_by_task.values() for run in runs if run.get("submitted_by_user_id") is not None
    }
    submitter_names = {
        u["_id"]: (u.get("display_name") or u.get("username"))
        for u in db.users.find({"_id": {"$in": list(submitter_ids)}}, {"display_name": 1, "username": 1})
    }

    # One query for every score across every requested task — reused below
    # both for "has THIS viewer judged it" (per task+profile) and for the
    # community-rating aggregate (per task+profile+harness, everyone).
    scores_by_task: dict[str, list[dict]] = defaultdict(list)
    for s in db.scores.find({"task_id": {"$in": task_ids}, "is_deleted": {"$ne": True}}):
        scores_by_task[s["task_id"]].append(s)

    # One query for every judge verdict across every requested task.
    verdicts_by_task: dict[str, dict[str, dict]] = defaultdict(dict)
    for v in db.judge_verdicts.find({"task_id": {"$in": task_ids}, "is_deleted": {"$ne": True}}):
        verdicts_by_task[v["task_id"]][v["harness_key"]] = v

    if adapters is None:
        adapters = all_adapters(db)

    out: dict[str, TaskOverviewOut] = {}
    for task_id in task_ids:
        task_runs = runs_by_task.get(task_id, [])  # already sorted desc by _id
        current_by_harness: dict[str, dict] = {}
        for run in task_runs:
            current_by_harness.setdefault(run["harness_key"], run)
        current_asc = sorted(current_by_harness.values(), key=lambda r: r["_id"])

        # Every score for this task, indexed by (harness_key,
        # provider_config_id) — reused for EVERY run below, not just the
        # resolved profile's: a superseded round (Battle Log's standalone
        # past-round cards) is still a real, judgeable comparison, just not
        # "the current one" for this task, and it deserves its own real
        # judged/community status rather than a placeholder. Already have
        # every score for the task loaded (scores_by_task above) — this is
        # just a second grouping of the same in-memory list, no new query.
        my_score_by_key: dict[tuple[str, int | None], float] = {}
        community_by_key: dict[tuple[str, int | None], list[float]] = defaultdict(list)
        for s in scores_by_task.get(task_id, []):
            key = (s["harness_key"], s.get("provider_config_id"))
            community_by_key[key].append(s["value"])
            if user and s.get("user_id") == user["_id"]:
                my_score_by_key[key] = s["value"]

        def out_for(run: dict) -> RunOut:
            key = (run["harness_key"], run.get("provider_config_id"))
            votes = community_by_key.get(key, [])
            return _run_out(
                run,
                db,
                user,
                deliverables=deliverables_by_run.get(run["_id"], []),
                submitted_by=submitter_names.get(run.get("submitted_by_user_id")),
                already_scored=my_score_by_key.get(key),
                community_avg_score=round(sum(votes) / len(votes), 2) if votes else None,
                community_vote_count=len(votes),
                ondemand_labels=ondemand_labels,
            )

        resolved_profile = _resolve_ever_done_provider_config_id(current_asc, task_runs)
        # Scope the comparison to ONE round, not merely to one profile.
        # provider_config_id alone cannot separate two battles that reused
        # the same profile — which is the normal case — so grouping only by
        # it silently splices a harness from an older round into the newest
        # one: run a deliberate Claude Code + Codex battle, and OnDemand's
        # `done` run from a previous round (same profile) still gets picked
        # up as "latest done for its harness" and shown as part of a round
        # it was never in, with the newest round's own id on the card. The
        # round is what a battle actually IS (see runner.py's round_id, one
        # per trigger), so resolve which round the comparison is, then take
        # only that round's runs. Runs predating round_id fall back to the
        # old profile-only behavior below, so they still compare as before.
        resolved_round = next(
            (
                run.get("round_id")
                for run in task_runs  # newest first
                if run["status"] == "done"
                and run.get("provider_config_id") == resolved_profile
                and run.get("round_id") is not None
            ),
            None,
        )
        done_runs_for_profile = []
        seen_harnesses: set[str] = set()
        for run in task_runs:  # newest first, so the first per harness is "latest"
            if run["status"] != "done" or run.get("provider_config_id") != resolved_profile:
                continue
            # Only the resolved round's runs — except for legacy runs with no
            # round_id at all, which have no round to be scoped to.
            if resolved_round is not None and run.get("round_id") != resolved_round:
                continue
            if run["harness_key"] in seen_harnesses:
                continue
            seen_harnesses.add(run["harness_key"])
            done_runs_for_profile.append(run)
        done_runs_for_profile.sort(key=lambda r: r["_id"])

        if done_runs_for_profile:
            existing_scores = (
                {
                    s["harness_key"]: s
                    for s in scores_by_task.get(task_id, [])
                    if s.get("user_id") == user["_id"] and s.get("provider_config_id") == resolved_profile
                }
                if user
                else {}
            )
            revealed = len(existing_scores) > 0
            verdicts = verdicts_by_task.get(task_id, {}) if revealed else {}
            by_harness_values: dict[str, list[float]] = defaultdict(list)
            for s in scores_by_task.get(task_id, []):
                if s.get("provider_config_id") == resolved_profile:
                    by_harness_values[s["harness_key"]].append(s["value"])
            community_stats = {hk: (round(sum(vals) / len(vals), 2), len(vals)) for hk, vals in by_harness_values.items()}

            compare_entries = build_compare_entries(
                done_runs_for_profile,
                existing_scores,
                verdicts,
                community_stats,
                revealed,
                adapters if revealed else {},
                lambda run_id: deliverables_by_run.get(run_id, []),
                lambda run_id: website_and_extra_ids(
                    deliverables_by_run.get(run_id, []),
                    package_json_by_run.get(run_id, {}),
                    expected_deliverables_by_task.get(task_id, ""),
                ),
            )
            compare_out = CompareOut(task_id=task_id, revealed=revealed, entries=compare_entries)
        else:
            compare_out = CompareOut(task_id=task_id, revealed=False, entries=[])

        run_outs = {run["_id"]: out_for(run) for run in task_runs}
        out[task_id] = TaskOverviewOut(
            task_id=task_id,
            runs=[run_outs[r["_id"]] for r in current_asc],
            history=[run_outs[r["_id"]] for r in task_runs],
            compare=compare_out,
        )
    return out


_BOARD_STATUS_VALUES = {
    "Queued", "In progress", "Retrying failed runs", "Partially failed", "Failed",
    "Insufficient results to judge", "Judged", "Awaiting your judgement",
    "Awaiting community & your judgement",
}
_BOARD_OUTCOME_VALUES = {"Decisive", "Tie"}


def _resolve_row_status(*, running: bool, queued: bool, retrying: bool, done_count: int, has_failed: bool, judged: bool, not_judged_status: str) -> str:
    """Port of BattleLog.jsx's resolveRowStatus — kept byte-for-byte in
    sync with it since GET /api/runs/board's status filter has to match
    the same values the FE dropdown/cards use."""
    if retrying and (running or queued):
        return "Retrying failed runs"
    if running:
        return "In progress"
    if queued:
        return "Queued"
    if done_count == 0:
        return "Failed" if has_failed else "Not run"
    if done_count == 1:
        return "Insufficient results to judge"
    if has_failed:
        return "Partially failed"
    return "Judged" if judged else not_judged_status


_DT_MIN = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
_BOARD_CACHE_TTL_SECONDS = 45
_BOARD_INDEX_RUN_FIELDS = {
    "_id": 1,
    "task_id": 1,
    "harness_key": 1,
    "round_id": 1,
    "status": 1,
    "started_at": 1,
    "finished_at": 1,
    "is_retrying": 1,
    "provider_config_id": 1,
}


def _latest_run_at(runs: list[RunOut]) -> dt.datetime | None:
    timestamps = [r.finished_at or r.started_at for r in runs]
    timestamps = [t for t in timestamps if t is not None]
    return max(timestamps) if timestamps else None


def _round_key(run: dict) -> str:
    return str(run["round_id"]) if run.get("round_id") is not None else f"run-{run['_id']}"


def _parse_cached_dt(value) -> dt.datetime | None:
    if value is None or isinstance(value, dt.datetime):
        return value
    if isinstance(value, str):
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def _build_board_index(task_ids: list[str], db: Database) -> list[dict]:
    """Cheap per-round rows used to filter/sort/paginate BEFORE the full
    overview pipeline. Status/outcome still overlay the viewer's scores
    in `runs_board`; this only carries run grouping + community votes."""
    if not task_ids:
        return []
    runs_by_task: dict[str, list[dict]] = defaultdict(list)
    for run in db.runs.find(
        {"task_id": {"$in": task_ids}, "is_deleted": {"$ne": True}},
        _BOARD_INDEX_RUN_FIELDS,
    ).sort("_id", -1):
        runs_by_task[run["task_id"]].append(run)
    if not runs_by_task:
        return []

    community_counts: dict[str, dict[tuple, int]] = defaultdict(lambda: defaultdict(int))
    for score in db.scores.find(
        {"task_id": {"$in": list(runs_by_task)}, "is_deleted": {"$ne": True}},
        {"task_id": 1, "harness_key": 1, "provider_config_id": 1},
    ):
        community_counts[score["task_id"]][(score["harness_key"], score.get("provider_config_id"))] += 1

    index: list[dict] = []
    for task_id, task_runs in runs_by_task.items():
        grouped: dict[str, list[dict]] = defaultdict(list)
        for run in task_runs:
            grouped[_round_key(run)].append(run)
        counts = community_counts.get(task_id, {})
        for key, group_runs in grouped.items():
            sorted_runs = sorted(group_runs, key=lambda r: r["_id"])
            done_runs = [r for r in sorted_runs if r["status"] == "done"]
            progress_runs = [r for r in sorted_runs if r["status"] in ("pending", "running")]
            failed_runs = [r for r in sorted_runs if r["status"] in ("error", "stopped")]
            done_pairs = [(r["harness_key"], r.get("provider_config_id")) for r in done_runs]
            timestamps = [r.get("finished_at") or r.get("started_at") for r in sorted_runs]
            timestamps = [t for t in timestamps if t is not None]
            index.append(
                {
                    "task_id": task_id,
                    "row_key": f"{task_id}#{key}",
                    "latest_run_at": max(timestamps) if timestamps else None,
                    "running": any(r["status"] == "running" for r in progress_runs),
                    "queued": any(r["status"] == "pending" for r in progress_runs),
                    "retrying": any(bool(r.get("is_retrying")) for r in progress_runs),
                    "done_count": len(done_runs),
                    "has_failed": len(failed_runs) > 0,
                    "community_judged": any(counts.get(pair, 0) > 0 for pair in done_pairs),
                    "done_pairs": [list(pair) for pair in done_pairs],
                }
            )
    return index


def _index_status(row: dict, my_scores: dict[tuple, float]) -> tuple[str, str | None]:
    judged_scores = []
    for pair in row.get("done_pairs") or []:
        harness_key, profile_id = pair[0], pair[1] if len(pair) > 1 else None
        score = my_scores.get((row["task_id"], harness_key, profile_id))
        if score is not None:
            judged_scores.append(score)
    judged = bool(judged_scores)
    not_judged_status = (
        "Awaiting your judgement" if row.get("community_judged") else "Awaiting community & your judgement"
    )
    status = _resolve_row_status(
        running=bool(row.get("running")),
        queued=bool(row.get("queued")),
        retrying=bool(row.get("retrying")),
        done_count=int(row.get("done_count") or 0),
        has_failed=bool(row.get("has_failed")),
        judged=judged,
        not_judged_status=not_judged_status,
    )
    outcome = None
    if judged and len(judged_scores) >= 2:
        ranked = sorted(judged_scores, reverse=True)
        outcome = "Tie" if sum(1 for value in ranked if value == ranked[0]) > 1 else "Decisive"
    return status, outcome


def _board_rows_for_task(task: TaskOut, overview: TaskOverviewOut | None, harness_names: dict[str, str]) -> list[BoardRowOut]:
    """Port of BattleLog.jsx's buildRows — one row per round_id, or one
    'Not run' placeholder when the task has no runs/overview at all."""
    board_task = BoardTaskOut.model_validate(task, from_attributes=True)
    if overview is None:
        return [BoardRowOut(task=board_task, row_key=f"{task.id_aa}#empty", status="Not run", is_primary_card=True)]

    all_runs_by_id = {r.id: r for r in [*overview.history, *overview.runs]}
    grouped: dict[str, list[RunOut]] = defaultdict(list)
    for run in all_runs_by_id.values():
        grouped[str(run.round_id) if run.round_id is not None else f"run-{run.id}"].append(run)
    if not grouped:
        return [BoardRowOut(task=board_task, row_key=f"{task.id_aa}#empty", status="Not run", is_primary_card=True)]

    compare_by_run_id = {e.run_id: e for e in overview.compare.entries}
    rows: list[BoardRowOut] = []
    for key, group_runs in grouped.items():
        sorted_runs = sorted(group_runs, key=lambda r: r.id)
        done_runs = [r for r in sorted_runs if r.status == "done"]
        progress_runs = [r for r in sorted_runs if r.status in ("pending", "running")]
        failed_runs = [r for r in sorted_runs if r.status in ("error", "stopped")]
        running = any(r.status == "running" for r in progress_runs)
        queued = any(r.status == "pending" for r in progress_runs)
        retrying = any(r.is_retrying for r in progress_runs)

        entries = []
        for run in done_runs:
            merged = run.model_dump()
            compare_entry = compare_by_run_id.get(run.id)
            if compare_entry is not None:
                merged.update(compare_entry.model_dump())
            # CompareEntry's harness_key/harness_name are anonymized (None)
            # until the viewer has judged this round — right for the blind
            # Judge screen, wrong here: Battle Log/Evaluate cards always show
            # real identity. Re-assert the real run values last, same order
            # buildRows() in BattleLog.jsx uses (spread comparison, then
            # override harness_key/harness_name from the run itself).
            merged["harness_key"] = run.harness_key
            merged["harness_name"] = harness_names.get(run.harness_key, run.harness_key)
            merged["run_id"] = run.id
            merged["round_id"] = run.round_id
            merged["score"] = merged.get("already_scored")
            entries.append(merged)
        progress_entries = [
            {
                "run_id": r.id, "round_id": r.round_id, "harness_key": r.harness_key,
                "harness_name": harness_names.get(r.harness_key, r.harness_key), "model": r.model,
                "done": r.deliverables_done, "expected": r.deliverables_expected, "status": r.status,
                "retrying": r.is_retrying, "can_stop": r.can_stop, "submitted_by": r.submitted_by,
            }
            for r in progress_runs
        ]
        failed_entries = [
            {
                "run_id": r.id, "round_id": r.round_id, "harness_key": r.harness_key,
                "harness_name": harness_names.get(r.harness_key, r.harness_key), "model": r.model,
                "status": r.status, "error_message": r.error_message or ("Run stopped." if r.status == "stopped" else ""),
                "can_retry": r.can_retry, "submitted_by": r.submitted_by,
            }
            for r in failed_runs
        ]

        judged = any(e.get("score") is not None for e in entries)
        community_judged = any((e.get("community_vote_count") or 0) > 0 for e in entries)
        not_judged_status = "Awaiting your judgement" if community_judged else "Awaiting community & your judgement"
        scored = sorted((e for e in entries if e.get("score") is not None), key=lambda e: -e["score"])
        outcome, margin = None, 0.0
        if judged and len(scored) >= 2:
            top = scored[0]["score"]
            tie = sum(1 for e in scored if e["score"] == top) > 1
            outcome = "Tie" if tie else "Decisive"
            margin = 0.0 if tie else round(top - scored[1]["score"], 1)

        rows.append(
            BoardRowOut(
                task=board_task,
                row_key=f"{task.id_aa}#{key}",
                round_id=sorted_runs[0].round_id,
                status=_resolve_row_status(
                    running=running, queued=queued, retrying=retrying, done_count=len(entries),
                    has_failed=len(failed_entries) > 0, judged=judged, not_judged_status=not_judged_status,
                ),
                outcome=outcome,
                margin=margin,
                latest_run_at=_latest_run_at(sorted_runs),
                entries=scored if judged else entries,
                progress_entries=progress_entries,
                failed_entries=failed_entries,
            )
        )
    # Primary card = the row with the newest activity, same tiebreak
    # (row_key ascending) buildRows applies before marking rows[0].
    rows.sort(key=lambda r: r.row_key)
    rows.sort(key=lambda r: r.latest_run_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
    rows[0].is_primary_card = True
    return rows


@router.get("/board", response_model=RunsBoardOut)
def runs_board(
    response: Response,
    category: str | None = None,
    group: str | None = None,
    include_deleted: bool = False,
    status: str | None = None,
    outcome: str | None = None,
    sort: str = "desc",
    page: int = 1,
    limit: int = 6,
    db: Database = Depends(get_db),
    user: dict | None = Depends(current_user),
):
    """Combined, filtered, paginated task+overview endpoint — replaces the
    GET /api/tasks + POST /api/runs/overview pair that BattleLog/Evaluate/
    Benchmark each do today. Unlike paginating /api/tasks alone, status/
    outcome/sort are resolved here (same rules as buildRows/resolveRowStatus
    on the FE) BEFORE pagination, so a page always has exactly `limit`
    matching rows instead of some tasks silently failing a client-side
    filter after the page was already picked.
    """
    if status is not None and status not in _BOARD_STATUS_VALUES:
        raise HTTPException(status_code=400, detail=f"unknown status filter: {status!r}")
    if outcome is not None and outcome not in _BOARD_OUTCOME_VALUES:
        raise HTTPException(status_code=400, detail=f"unknown outcome filter: {outcome!r}")
    if sort not in ("asc", "desc"):
        raise HTTPException(status_code=400, detail=f"unknown sort: {sort!r}")
    page = max(1, page)
    limit = max(1, min(limit, MAX_TASKS_PAGE_LIMIT))

    admin_viewer = is_admin(user)
    cacheable = not include_deleted and not admin_viewer
    viewer_key = "anon" if user is None else f"u:{user['_id']}"
    page_variant = (
        f"page:group:{group or ''}:category:{category or ''}:status:{status or ''}:"
        f"outcome:{outcome or ''}:sort:{sort}:page:{page}:limit:{limit}:viewer:{viewer_key}"
    )
    if cacheable:
        cached = cache_get("runs_board", page_variant)
        if cached is not None:
            cache_mark(response, hit=True)
            return cached
        cache_mark(response, hit=False)
    else:
        cache_mark(response, hit=False)

    # A cache hit inside list_tasks returns plain cached dicts, not TaskOut
    # instances (that coercion normally happens via FastAPI's response_model
    # when the route runs through HTTP, which calling the function directly
    # skips) — normalize both cases the same way here.
    raw_tasks = _list_tasks_for_board(Response(), category=category, group=group, include_deleted=include_deleted, page=1, limit=None, lean=True, db=db, user=user)
    tasks = [t if isinstance(t, TaskOut) else TaskOut.model_validate(t) for t in raw_tasks]
    task_by_id = {t.id_aa: t for t in tasks}

    index_variant = f"index:group:{group or ''}:category:{category or ''}"
    index = None if include_deleted else cache_get("runs_board", index_variant)
    if index is not None:
        for row in index:
            row["latest_run_at"] = _parse_cached_dt(row.get("latest_run_at"))
    else:
        index = _build_board_index([t.id_aa for t in tasks], db)
        if not include_deleted:
            cache_set("runs_board", index, variant=index_variant, ttl_seconds=_BOARD_CACHE_TTL_SECONDS)

    my_scores: dict[tuple, float] = {}
    if user is not None:
        for score in db.scores.find(
            {"user_id": user["_id"], "is_deleted": {"$ne": True}},
            {"task_id": 1, "harness_key": 1, "provider_config_id": 1, "value": 1},
        ):
            my_scores[(score["task_id"], score["harness_key"], score.get("provider_config_id"))] = score["value"]

    ranked: list[dict] = []
    for row in index:
        if row["task_id"] not in task_by_id:
            continue
        row_status, row_outcome = _index_status(row, my_scores)
        if row_status == "Not run":
            continue
        if status and row_status != status:
            continue
        if outcome and row_outcome != outcome:
            continue
        ranked.append(row)

    ranked.sort(key=lambda r: r["row_key"])
    ranked.sort(key=lambda r: r["latest_run_at"] or _DT_MIN, reverse=(sort == "desc"))
    total = len(ranked)
    start = (page - 1) * limit
    page_index = ranked[start : start + limit]
    page_task_ids = list(dict.fromkeys(row["task_id"] for row in page_index))

    adapters = all_adapters(db)
    overviews = _build_overviews(page_task_ids, db, user, adapters=adapters)
    harness_names = {key: adapter.name for key, adapter in adapters.items()}
    by_key: dict[str, BoardRowOut] = {}
    for task_id in page_task_ids:
        for built in _board_rows_for_task(task_by_id[task_id], overviews.get(task_id), harness_names):
            by_key[built.row_key] = built
    rows = [by_key[row["row_key"]] for row in page_index if row["row_key"] in by_key]
    out = RunsBoardOut(rows=rows, total=total, page=page, limit=limit)
    if cacheable:
        cache_set("runs_board", out, variant=page_variant, ttl_seconds=_BOARD_CACHE_TTL_SECONDS)
    return out


@router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: int, db: Database = Depends(get_db), user: dict | None = Depends(current_user)):
    run = db.runs.find_one({"_id": run_id})
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _run_out(run, db, user)


# How often the SSE stream below re-checks the run document for changes.
LOG_STREAM_POLL_SECONDS = 1.0


@router.get("/{run_id}/logs/stream")
async def stream_run_log(
    run_id: int,
    token: str | None = Query(default=None, description="Auth token — only needed because browser EventSource can't set X-User-Token."),
    x_user_token: str | None = Header(default=None),
    db: Database = Depends(get_db),
):
    """Server-Sent Events stream of a run's live log — one event whenever
    `raw_log`/status/progress changes, polling the run document underneath
    (see runner.py's persist_live_log for how raw_log fills in while a
    harness is still running). Works for a run in any state: a pending run
    streams until it starts producing output, a finished run gets one event
    with the final state and the stream closes immediately.

    Open to any authenticated user — not scoped to the caller's own runs,
    same as `GET /{run_id}` itself. Accepts the auth token via `?token=`
    as well as the usual `X-User-Token` header: this is the one endpoint a
    browser calls with a plain `EventSource`, which cannot set custom
    headers, so it needs a URL-based fallback. `x_user_token` still wins
    when both are present (a manual fetch-based SSE client that *can* send
    the header shouldn't be second-guessed by a stray query param)."""
    user = current_user(x_user_token or token, db)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in to continue")

    async def event_gen():
        last_payload = None
        while True:
            run = db.runs.find_one(
                {"_id": run_id},
                {"raw_log": 1, "status": 1, "deliverables_done": 1, "deliverables_expected": 1, "error_message": 1},
            )
            if run is None:
                yield "event: error\ndata: run not found\n\n"
                return
            payload = json.dumps(
                {
                    "status": run.get("status"),
                    "raw_log": run.get("raw_log", ""),
                    "deliverables_done": run.get("deliverables_done", 0),
                    "deliverables_expected": run.get("deliverables_expected", 0),
                    "error_message": _public_error_message(run, user),
                }
            )
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload
            if run.get("status") in ("done", "error", "stopped"):
                return
            await asyncio.sleep(LOG_STREAM_POLL_SECONDS)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.get("/deliverable/{deliverable_id}/content")
def get_deliverable_content(deliverable_id: int, inline: bool = False, db: Database = Depends(get_db)):
    """`inline=true` serves the file for in-page rendering (the browser's own
    PDF viewer) instead of as a download. A plain `attachment` disposition
    makes an embedded viewer download the file instead of displaying it —
    so the inline path deliberately uses `inline` instead."""
    d = db.deliverables.find_one({"_id": deliverable_id})
    if d is None:
        raise HTTPException(status_code=404, detail="deliverable not found")
    disposition = "inline" if inline else "attachment"
    return Response(
        content=d["content"],
        media_type=d["media_type"],
        headers={"Content-Disposition": f'{disposition}; filename="{os.path.basename(d["filename"])}"'},
    )


@router.get("/deliverable/{deliverable_id}/preview")
def get_deliverable_preview(deliverable_id: int, db: Database = Depends(get_db)):
    """Structured, size-capped rendering of a deliverable so it can be read
    inline in the judging UI (spreadsheet grids, rendered document HTML,
    slide text, the real PDF) rather than downloaded. See app/preview.py."""
    d = db.deliverables.find_one({"_id": deliverable_id})
    if d is None:
        raise HTTPException(status_code=404, detail="deliverable not found")
    return {
        "id": d["_id"],
        "filename": d["filename"],
        "media_type": d["media_type"],
        "size_bytes": d["size_bytes"],
        "preview": build_preview(d.get("content"), d["filename"]),
    }


@router.get("/deliverable/{deliverable_id}/pptx-preview.pdf")
def get_pptx_preview_pdf(deliverable_id: int, db: Database = Depends(get_db)):
    """Return a cached, private PDF rendering of a PowerPoint deliverable."""
    d = db.deliverables.find_one({"_id": deliverable_id})
    if d is None:
        raise HTTPException(status_code=404, detail="deliverable not found")
    if Path(d["filename"]).suffix.lower() != ".pptx":
        raise HTTPException(status_code=400, detail="deliverable is not a PowerPoint file")

    pdf = d.get("pptx_preview_pdf")
    if not pdf:
        try:
            pdf = render_pptx_as_pdf(d.get("content") or b"")
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        db.deliverables.update_one({"_id": deliverable_id}, {"$set": {"pptx_preview_pdf": pdf}})

    preview_name = f"{Path(d['filename']).stem}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{os.path.basename(preview_name)}"'},
    )
