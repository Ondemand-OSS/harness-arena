from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pymongo.database import Database

from ..cache import get_json as cache_get, mark_response as cache_mark, set_json as cache_set
from ..db import get_db
from ..harnesses.registry import enabled_harness_keys
from ..schemas import StatsOut

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("", response_model=StatsOut)
def get_stats(response: Response, db: Database = Depends(get_db)):
    """Counts behind the UI's "N tasks · N harnesses · N recorded runs"
    strips. `recorded_runs` counts only completed runs — a pending/errored
    run isn't a result anyone can review, so counting it would overstate
    what's actually available to judge."""
    cached = cache_get("stats")
    if cached is not None:
        cache_mark(response, hit=True)
        return cached
    cache_mark(response, hit=False)

    tasks = db.tasks.count_documents({"is_deleted": {"$ne": True}})
    recorded_runs = db.runs.count_documents({"status": "done", "is_deleted": {"$ne": True}})
    judged_tasks = len(db.scores.distinct("task_id", {"is_deleted": {"$ne": True}}))
    categories = len([c for c in db.tasks.distinct("category", {"is_deleted": {"$ne": True}}) if c])
    models = len([m for m in db.provider_config.distinct("model") if m])

    out = StatsOut(
        tasks=tasks,
        harnesses=len(enabled_harness_keys(db)),
        models=models,
        recorded_runs=recorded_runs,
        judged_tasks=judged_tasks,
        categories=categories,
    )
    cache_set("stats", out, ttl_seconds=45)
    return out
