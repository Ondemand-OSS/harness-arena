from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field

from . import settings


class TaskCreate(BaseModel):
    agent: str
    message: str = Field(min_length=1, max_length=settings.MAX_MESSAGE_CHARS)
    file_ids: list[str] = Field(default_factory=list, max_length=settings.MAX_FILES_PER_TASK)


class FileOut(BaseModel):
    id: str
    name: str
    mime: str
    size: int
    url: str


class TaskOut(BaseModel):
    task_id: str
    agent: str
    status: str
    answer: str = ""
    files: list[FileOut] = Field(default_factory=list)
    error: str = ""
    created_at: dt.datetime
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None


class TaskListOut(BaseModel):
    tasks: list[TaskOut]


class AgentOut(BaseModel):
    key: str
    name: str
    tagline: str


class AgentListOut(BaseModel):
    agents: list[AgentOut]


class StatusOut(BaseModel):
    status: str
    agents: list[str]
    active_tasks: int
    max_concurrent_tasks: int
