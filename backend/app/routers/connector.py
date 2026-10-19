from __future__ import annotations

import os

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pymongo.database import Database

from ..connector import service, settings
from ..connector.schemas import AgentListOut, AgentOut, FileOut, StatusOut, TaskCreate, TaskListOut, TaskOut
from ..connector.security import require_connector_user
from ..connector.store import bucket, files_collection
from ..db import get_db
from ..harnesses.registry import all_adapters

router = APIRouter(prefix="/api/connector/v1", tags=["connector"])

UPLOAD_CHUNK_BYTES = 1024 * 1024
MAX_LIST_LIMIT = 100


def _base_url(request: Request) -> str:
    return settings.PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


def _file_out(request: Request, item: dict) -> FileOut:
    return FileOut(**item, url=f"{_base_url(request)}/api/connector/v1/files/{item['id']}")


def _task_out(request: Request, doc: dict) -> TaskOut:
    return TaskOut(
        task_id=doc["_id"],
        agent=doc["agent"],
        status=doc["status"],
        answer=doc.get("answer", ""),
        files=[_file_out(request, item) for item in doc.get("output_files", [])],
        error=doc.get("error", ""),
        created_at=doc["created_at"],
        started_at=doc.get("started_at"),
        finished_at=doc.get("finished_at"),
    )


def _owned_task(db: Database, user: dict, task_id: str) -> dict:
    doc = db.connector_tasks.find_one({"_id": task_id, "user_id": user["_id"]})
    if doc is None:
        raise HTTPException(status_code=404, detail="task not found")
    return doc


@router.get("/agents", response_model=AgentListOut)
def list_agents(user: dict = Depends(require_connector_user("chat")), db: Database = Depends(get_db)):
    adapters = all_adapters(db)
    agents = [
        AgentOut(key=key, name=adapters[key].name, tagline=adapters[key].tagline)
        for key in settings.ENABLED_AGENTS
        if key in adapters
    ]
    return AgentListOut(agents=agents)


@router.get("/status", response_model=StatusOut)
def status(user: dict = Depends(require_connector_user("chat")), db: Database = Depends(get_db)):
    return StatusOut(
        status="ok",
        agents=settings.ENABLED_AGENTS,
        active_tasks=service.active_task_count(db, user["_id"]),
        max_concurrent_tasks=settings.MAX_CONCURRENT_TASKS,
    )


@router.post("/files", response_model=FileOut, status_code=201)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(require_connector_user("files")),
    db: Database = Depends(get_db),
):
    name = os.path.basename(file.filename or "") or "upload"
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > settings.MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"file exceeds {settings.MAX_UPLOAD_BYTES} bytes")
        chunks.append(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail="file is empty")
    stored = service.store_bytes(db, name, b"".join(chunks), user["_id"], "upload")
    return _file_out(request, stored)


@router.get("/files/{file_id}")
def download_file(
    file_id: str,
    user: dict = Depends(require_connector_user("files")),
    db: Database = Depends(get_db),
):
    object_id = service.parse_object_id(file_id)
    record = files_collection(db).find_one({"_id": object_id}) if object_id else None
    if record is None or (record.get("metadata") or {}).get("user_id") != user["_id"]:
        raise HTTPException(status_code=404, detail="file not found")
    stream = bucket(db).open_download_stream(object_id)
    name = os.path.basename(record["filename"]).replace('"', "")
    return StreamingResponse(
        iter(lambda: stream.read(UPLOAD_CHUNK_BYTES), b""),
        media_type=(record.get("metadata") or {}).get("mime", "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{name}"', "X-Content-Type-Options": "nosniff"},
    )


@router.post("/tasks", response_model=TaskOut, status_code=202)
async def create_task(
    body: TaskCreate,
    request: Request,
    user: dict = Depends(require_connector_user("chat")),
    db: Database = Depends(get_db),
):
    if body.agent not in settings.ENABLED_AGENTS or body.agent not in all_adapters(db):
        raise HTTPException(status_code=400, detail="unknown or disabled agent")
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="message is empty")
    if service.active_task_count(db, user["_id"]) >= settings.MAX_ACTIVE_TASKS_PER_USER:
        raise HTTPException(status_code=429, detail="too many active tasks; wait for one to finish")
    file_ids = []
    for raw in dict.fromkeys(body.file_ids):
        object_id = service.parse_object_id(raw)
        record = files_collection(db).find_one({"_id": object_id}) if object_id else None
        metadata = (record or {}).get("metadata") or {}
        if record is None or metadata.get("user_id") != user["_id"] or metadata.get("kind") != "upload":
            raise HTTPException(status_code=400, detail=f"unknown file id: {raw}")
        file_ids.append(object_id)
    task_id = service.create_task(db, user["_id"], body.agent, body.message, file_ids)
    return _task_out(request, db.connector_tasks.find_one({"_id": task_id}))


@router.get("/tasks", response_model=TaskListOut)
def list_tasks(
    request: Request,
    limit: int = 20,
    status: str | None = None,
    user: dict = Depends(require_connector_user("chat")),
    db: Database = Depends(get_db),
):
    query: dict = {"user_id": user["_id"]}
    if status:
        query["status"] = status
    cursor = db.connector_tasks.find(query).sort("created_at", -1).limit(max(1, min(limit, MAX_LIST_LIMIT)))
    return TaskListOut(tasks=[_task_out(request, doc) for doc in cursor])


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(
    task_id: str,
    request: Request,
    user: dict = Depends(require_connector_user("chat")),
    db: Database = Depends(get_db),
):
    return _task_out(request, _owned_task(db, user, task_id))


@router.delete("/tasks/{task_id}", response_model=TaskOut)
def cancel_task(
    task_id: str,
    request: Request,
    user: dict = Depends(require_connector_user("chat")),
    db: Database = Depends(get_db),
):
    _owned_task(db, user, task_id)
    service.request_cancel(db, task_id)
    return _task_out(request, _owned_task(db, user, task_id))
