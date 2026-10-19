from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import mimetypes
import os
import shutil
import tempfile
import types
import uuid

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.database import Database

from ..db import get_client
from ..harnesses.registry import get_adapter
from ..logger import get_logger, log_error
from ..mongo import MONGODB_DB_NAME
from ..runner import RECONCILE_INTERVAL_SECONDS, get_provider_settings, hold_lease, new_lease_fields, stale_lease_filter
from . import settings
from .store import bucket, files_collection, utcnow

log = get_logger("connector")

ACTIVE_STATUSES = ["pending", "running"]
ANSWER_FILENAME = "answer.md"
SLOT_POLL_INTERVAL_SECONDS = 2.0
CANCEL_POLL_INTERVAL_SECONDS = 2.0
ERROR_MAX_CHARS = 600
IGNORED_OUTPUTS = {
    "openclaw": {
        "agents.md",
        "bootstrap.md",
        "heartbeat.md",
        "identity.md",
        "soul.md",
        "tools.md",
        "user.md",
        "openclaw-workspace-state.json",
    },
}

_BACKGROUND: dict[str, asyncio.Task] = {}

PROMPT_SUFFIX = (
    "\n\n---\n"
    f"When you are finished, write your complete final reply to a file named {ANSWER_FILENAME} in the "
    "current working directory. Save any other files you produce in the current working directory too."
)


def parse_object_id(value: str) -> ObjectId | None:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def active_task_count(db: Database, user_id: int | None = None) -> int:
    query: dict = {"status": {"$in": ACTIVE_STATUSES}}
    if user_id is not None:
        query["user_id"] = user_id
    return db.connector_tasks.count_documents(query)


def store_bytes(db: Database, filename: str, content: bytes, user_id: int, kind: str, task_id: str = "") -> dict:
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    file_id = bucket(db).upload_from_stream(
        filename,
        content,
        metadata={"user_id": user_id, "kind": kind, "task_id": task_id, "mime": mime, "size": len(content)},
    )
    return {"id": str(file_id), "name": filename, "mime": mime, "size": len(content)}


def create_task(db: Database, user_id: int, agent: str, message: str, file_ids: list[ObjectId]) -> str:
    task_id = uuid.uuid4().hex
    db.connector_tasks.insert_one(
        {
            "_id": task_id,
            "user_id": user_id,
            "agent": agent,
            "message": message,
            "input_file_ids": file_ids,
            "status": "pending",
            "answer": "",
            "output_files": [],
            "error": "",
            "cancel_requested": False,
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
            **new_lease_fields(),
        }
    )
    runner = asyncio.create_task(_run_task(task_id))
    _BACKGROUND[task_id] = runner
    runner.add_done_callback(lambda finished: _BACKGROUND.pop(task_id, None))
    return task_id


def request_cancel(db: Database, task_id: str) -> None:
    db.connector_tasks.update_one(
        {"_id": task_id, "status": {"$in": ACTIVE_STATUSES}}, {"$set": {"cancel_requested": True}}
    )


def _finish(db: Database, task_id: str, status: str, **fields) -> None:
    db.connector_tasks.update_one(
        {"_id": task_id, "status": {"$in": ACTIVE_STATUSES}},
        {"$set": {"status": status, "finished_at": utcnow(), **fields}},
    )


def _cancel_requested(db: Database, task_id: str) -> bool:
    doc = db.connector_tasks.find_one({"_id": task_id}, {"cancel_requested": 1})
    return bool((doc or {}).get("cancel_requested"))


async def _acquire_slot(db: Database, task_id: str) -> bool:
    while True:
        if _cancel_requested(db, task_id):
            return False
        if db.connector_tasks.count_documents({"status": "running"}) < settings.MAX_CONCURRENT_TASKS:
            claimed = db.connector_tasks.update_one(
                {"_id": task_id, "status": "pending"},
                {"$set": {"status": "running", "started_at": utcnow()}},
            )
            return claimed.modified_count == 1
        await asyncio.sleep(SLOT_POLL_INTERVAL_SECONDS)


def _load_attachments(db: Database, file_ids: list[ObjectId]) -> list[dict]:
    blobs: list[dict] = []
    seen: set[str] = set()
    for file_id in file_ids:
        try:
            stream = bucket(db).open_download_stream(file_id)
        except Exception:
            continue
        name = os.path.basename(stream.filename or "") or f"file-{file_id}"
        if name in seen:
            name = f"{file_id}-{name}"
        seen.add(name)
        blobs.append({"filename": name, "content": stream.read()})
    return blobs


def _collect_outputs(
    db: Database, workdir: str, relpaths: list[str], user_id: int, task_id: str, ignored: set[str]
) -> tuple[str, list[dict]]:
    root = os.path.realpath(workdir)
    answer = ""
    outputs: list[dict] = []
    for relpath in relpaths:
        if relpath.lower() in ignored:
            continue
        path = os.path.realpath(os.path.join(workdir, relpath))
        if os.path.commonpath((root, path)) != root or not os.path.isfile(path):
            continue
        with open(path, "rb") as handle:
            content = handle.read()
        if os.path.basename(relpath).lower() == ANSWER_FILENAME and not answer:
            answer = content.decode("utf-8", errors="replace")
            continue
        outputs.append(store_bytes(db, relpath, content, user_id, "output", task_id))
    return answer, outputs


async def _run_adapter(db: Database, task_id: str, adapter, task, workdir: str, provider):
    runner = asyncio.create_task(adapter.run(task, workdir, provider))
    try:
        while True:
            done, _pending = await asyncio.wait({runner}, timeout=CANCEL_POLL_INTERVAL_SECONDS)
            if done:
                return runner.result()
            if _cancel_requested(db, task_id):
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner
                return None
    except asyncio.CancelledError:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await runner
        raise


async def _run_task(task_id: str) -> None:
    db = get_client()[MONGODB_DB_NAME]
    async with hold_lease(db, "connector_tasks", task_id):
        try:
            await _execute(db, task_id)
        except asyncio.CancelledError:
            _finish(db, task_id, "error", error="Task was interrupted.")
            raise
        except Exception as exc:
            log.error("connector task %s crashed: %r", task_id, exc, exc_info=True)
            log_error(db, action="CONNECTOR_TASK", message=f"connector task {task_id} crashed", error=exc, metadata={"task_id": task_id})
            _finish(db, task_id, "error", error="Internal error while running the task.")


async def _execute(db: Database, task_id: str) -> None:
    task_doc = db.connector_tasks.find_one({"_id": task_id})
    if task_doc is None:
        return
    if not await _acquire_slot(db, task_id):
        if _cancel_requested(db, task_id):
            _finish(db, task_id, "cancelled", error="Cancelled by user.")
        return

    provider = get_provider_settings(db, settings.PROVIDER_CONFIG_ID)
    if not provider.api_key:
        _finish(db, task_id, "error", error="The connector has no model provider configured.")
        return
    try:
        adapter = get_adapter(db, task_doc["agent"])
    except KeyError:
        _finish(db, task_id, "error", error="Unknown agent.")
        return

    blobs = await asyncio.to_thread(_load_attachments, db, task_doc.get("input_file_ids", []))
    task = types.SimpleNamespace(
        title="Request",
        prompt=task_doc["message"] + PROMPT_SUFFIX,
        expected_deliverables="",
        system_prompt="",
        reference_files="",
        reference_file_blobs=blobs,
    )
    workdir = tempfile.mkdtemp(prefix=f"connector-task-{task_id[:8]}-")
    try:
        result = await _run_adapter(db, task_id, adapter, task, workdir, provider)
        if result is None:
            _finish(db, task_id, "cancelled", error="Cancelled by user.")
            return
        if not result.ok:
            _finish(db, task_id, "error", error=(result.error_message or "The agent failed.")[:ERROR_MAX_CHARS])
            return
        answer, outputs = await asyncio.to_thread(
            _collect_outputs,
            db,
            workdir,
            result.deliverables,
            task_doc["user_id"],
            task_id,
            IGNORED_OUTPUTS.get(task_doc["agent"], set()),
        )
        for prefix in {workdir, os.path.realpath(workdir)}:
            answer = answer.replace(prefix, ".")
        if not answer and not outputs:
            _finish(db, task_id, "error", error="The agent finished without writing a reply or any files.")
            return
        _finish(db, task_id, "done", answer=answer, output_files=outputs)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def purge_expired(db: Database) -> None:
    cutoff = utcnow() - dt.timedelta(days=settings.RETENTION_DAYS)
    stored = bucket(db)
    for task in db.connector_tasks.find({"finished_at": {"$lt": cutoff}}, {"output_files": 1}):
        for item in task.get("output_files", []):
            file_id = parse_object_id(item["id"])
            if file_id is not None:
                with contextlib.suppress(Exception):
                    stored.delete(file_id)
        db.connector_tasks.delete_one({"_id": task["_id"]})
    for orphan in files_collection(db).find({"uploadDate": {"$lt": cutoff}, "metadata.kind": "upload"}, {"_id": 1}):
        in_use = db.connector_tasks.count_documents({"input_file_ids": orphan["_id"]}, limit=1)
        if not in_use:
            with contextlib.suppress(Exception):
                stored.delete(orphan["_id"])


def reconcile_stale_tasks(db: Database) -> int:
    result = db.connector_tasks.update_many(
        {"status": {"$in": ACTIVE_STATUSES}, **stale_lease_filter()},
        {
            "$set": {
                "status": "error",
                "error": "The task was interrupted by a server restart. Please retry.",
                "finished_at": utcnow(),
            }
        },
    )
    return result.modified_count


async def sweep_loop() -> None:
    while True:
        try:
            db = get_client()[MONGODB_DB_NAME]
            reclaimed = reconcile_stale_tasks(db)
            if reclaimed:
                log.info("marked %d abandoned connector task(s) as failed", reclaimed)
            purge_expired(db)
        except Exception as exc:
            log.error("connector sweep failed: %s", exc, exc_info=True)
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
