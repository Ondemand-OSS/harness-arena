"""Live-preview endpoints for web-development runs.

A web-development run's deliverables are source files that only mean
something running (see webproject.py), so the judging UI offers "View
website" instead of a file-by-file reader. Deployment is lazy — nothing
is started until someone actually asks — and every failure degrades to
the same fallback: download the project as a zip and run it locally.

Kept in its own router rather than bolted onto runs.py because these
endpoints have a lifecycle of their own (starting billable sandboxes,
rather than reading rows that already exist).
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from pymongo.database import Database

from ..db import get_db
from ..logger import log_activity
from ..sandbox_deploy import (
    STATUS_DEPLOYING,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_LIVE,
    deployment_state,
    ensure_preview,
    load_run_files,
    preview_unavailable_reason,
)
from ..users import current_user, require_user
from ..webproject import build_zip, is_web_project

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/runs", tags=["deploy"])

# Strong refs to in-flight deploys, so asyncio (which holds only weak
# references to tasks) can't garbage-collect one mid-deploy. Same reason
# runner.py keeps its own set for background battles.
_deploy_tasks: set[asyncio.Task] = set()

# What a user is told when a preview can't be produced. The specific
# cause (missing CLI, npm failure, a build error in generated code) stays
# server-side: it's operator/admin diagnostics, and a judge's actual next
# step is the same either way — download the zip.
GENERIC_FAILURE = "This project couldn't be previewed. Download the source zip to run it locally."
# Expiry is not a failure and shouldn't read like one — the project still
# builds, its sandbox just timed out, and one click rebuilds it. Stated
# plainly so the rebuild wait is understood rather than looking like a
# hang (the same reason OnDemand's own viewer surfaces this explicitly
# instead of silently redeploying).
EXPIRED_MESSAGE = "This preview has expired. Redeploy the project to generate a fresh preview."


def _load_run_or_404(db: Database, run_id: int) -> dict:
    run = db.runs.find_one({"_id": run_id})
    if run is None or run.get("is_deleted"):
        raise HTTPException(status_code=404, detail="run not found")
    return run


def _task_for(db: Database, run: dict) -> dict:
    return db.tasks.find_one({"_id": run.get("task_id")}) or {}


def _require_web_run(db: Database, run: dict) -> None:
    task = _task_for(db, run)
    if not is_web_project(task.get("expected_deliverables", "")):
        raise HTTPException(
            status_code=400,
            detail="this task's output does not need building or running, so it has no live preview",
        )


@router.get("/{run_id}/preview")
def get_preview_status(run_id: int, db: Database = Depends(get_db), viewer: dict | None = Depends(current_user)):
    """Current preview state, without ever starting a deployment.

    The UI polls this while a deploy is in flight, and reads it on load to
    decide whether to show "View website" or a still-warming state."""
    run = _load_run_or_404(db, run_id)
    task = _task_for(db, run)
    deployment = run.get("deployment") or {}
    status = deployment_state(deployment)
    return {
        "run_id": run_id,
        # Drives whether the UI offers "View website" at all. False for a
        # plain-HTML task: the browser renders that itself, so it stays on
        # the ordinary file-viewer path (see webproject.is_web_project).
        "is_web_project": is_web_project(task.get("expected_deliverables", "")),
        "status": status,
        "preview_url": deployment.get("preview_url", "") if status == STATUS_LIVE else "",
        "expires_at": deployment.get("expires_at"),
        # Whether the harness hosted this preview itself ("harness", e.g.
        # OnDemand) or the arena deployed it ("arena"). A redeploy is
        # always ours regardless.
        "provider": deployment.get("provider", "arena"),
        "message": (
            EXPIRED_MESSAGE if status == STATUS_EXPIRED
            else GENERIC_FAILURE if status == STATUS_FAILED
            else ""
        ),
        # Admin-only diagnostics, same split as runs.py's raw_log.
        "error_detail": deployment.get("error", "") if (viewer and viewer.get("is_admin")) else None,
    }


@router.post("/{run_id}/preview")
async def start_preview(run_id: int, db: Database = Depends(get_db), user: dict = Depends(require_user)):
    """Start deploying this run's frontend, returning as soon as it's
    queued rather than when it's live.

    A cold deploy is `npm install` plus a build plus readiness polling —
    minutes, not seconds. Holding the HTTP request open that long is what
    the battle-trigger endpoint deliberately stopped doing (see
    runs.py's trigger_run): an intermediate proxy times the request out
    long before the work finishes, so the client sees a failure for a
    deploy that actually succeeded. The work runs in the background and
    the client polls GET /preview instead.

    Idempotent from the caller's perspective: a still-live sandbox is
    reused, an expired one is replaced, and a deploy already in flight is
    reported rather than duplicated (see sandbox_deploy.ensure_preview)."""
    run = _load_run_or_404(db, run_id)
    _require_web_run(db, run)
    if run.get("status") != "done":
        raise HTTPException(status_code=400, detail="only a completed run can be previewed")

    blocked = preview_unavailable_reason()
    if blocked:
        # A server-side misconfiguration, not a problem with this run —
        # still surfaced generically, with the real reason in the log.
        log_activity(
            db,
            action="RUN_PREVIEW_UNAVAILABLE",
            user_id=user["_id"],
            message=f"preview requested for run {run_id} but previews are unavailable: {blocked}",
            metadata={"run_id": run_id, "reason": blocked},
            route="/api/runs/{run_id}/preview",
        )
        return {"run_id": run_id, "status": STATUS_FAILED, "preview_url": "", "message": GENERIC_FAILURE}

    files = load_run_files(db, run_id)
    if not files:
        return {"run_id": run_id, "status": STATUS_FAILED, "preview_url": "", "message": GENERIC_FAILURE}

    # A sandbox that's still answering is returned right here — that's the
    # common "reopen a preview I already started" case and it needs no
    # background work or polling round-trip.
    existing = (run.get("deployment") or {})
    if existing.get("status") == STATUS_LIVE and existing.get("preview_url"):
        result = await ensure_preview(db, run_id, files)
        if result.get("status") == STATUS_LIVE:
            return {
                "run_id": run_id,
                "status": STATUS_LIVE,
                "preview_url": result.get("preview_url", ""),
                "expires_at": result.get("expires_at"),
                "message": "",
            }

    async def _deploy_in_background() -> None:
        try:
            result = await ensure_preview(db, run_id, files)
            log_activity(
                db,
                action="RUN_PREVIEW_DEPLOY",
                user_id=user["_id"],
                message=f"preview for run {run_id} finished with status {result.get('status')}",
                metadata={"run_id": run_id, "status": result.get("status"), "task_id": run.get("task_id")},
                route="/api/runs/{run_id}/preview",
            )
        except Exception:  # noqa: BLE001 - a background deploy must not die silently
            log.exception("background preview deploy crashed for run %s", run_id)

    task = asyncio.create_task(_deploy_in_background())
    _deploy_tasks.add(task)
    task.add_done_callback(_deploy_tasks.discard)

    return {
        "run_id": run_id,
        "status": STATUS_DEPLOYING,
        "preview_url": "",
        "expires_at": None,
        "message": "",
    }


@router.get("/{run_id}/project.zip")
def download_project_zip(run_id: int, db: Database = Depends(get_db)):
    """The whole generated project as a zip — the fallback whenever a
    preview isn't available, and a legitimate deliverable in its own right
    for a judge who'd rather read the source. Deliberately includes any
    backend files too, even though only the frontend is ever deployed."""
    run = _load_run_or_404(db, run_id)
    files = load_run_files(db, run_id)
    if not files:
        raise HTTPException(status_code=404, detail="this run has no files to download")
    task = _task_for(db, run)
    slug = str(task.get("_id") or run.get("task_id") or "project").replace("/", "-")
    return Response(
        content=build_zip(files),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{slug}-run{run_id}.zip"'},
    )
