from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median

from fastapi import APIRouter, Depends, HTTPException, Response
from pymongo.database import Database

from ..cache import get_json as cache_get, mark_response as cache_mark, set_json as cache_set
from ..db import get_db
from ..elo import compute_leaderboard
from ..harnesses.registry import all_adapters
from ..judge_stats import judge_summary, judge_summary_by_category
from ..schemas import LeaderboardRow
from ..taxonomy import DEFAULT_GROUP, GROUPS, group_for_category_with_approvals

router = APIRouter(prefix="/api/leaderboard", tags=["leaderboard"])


def _task_ids_for_group(db: Database, group: str) -> list[str]:
    approved_groups = {
        review["_id"]: review.get("group")
        for review in db.category_reviews.find({"status": "approved"}, {"group": 1})
        if review.get("group") in {*GROUPS, DEFAULT_GROUP}
    }
    return [
        task["_id"]
        for task in db.tasks.find({"is_deleted": {"$ne": True}}, {"category": 1})
        if group_for_category_with_approvals(task.get("category", ""), approved_groups) == group
    ]


@router.get("", response_model=list[LeaderboardRow])
def get_leaderboard(
    response: Response, category: str | None = None, group: str | None = None, db: Database = Depends(get_db)
):
    # Recomputing Elo from scratch over just this category's scores gives an
    # honest per-category ranking, rather than filtering a global-Elo table
    # after the fact (which would misrepresent rating gaps that were earned
    # against a different mix of opponents/tasks).
    if category and group:
        raise HTTPException(status_code=400, detail="choose either a category or a group")
    if group and group not in {*GROUPS, DEFAULT_GROUP}:
        raise HTTPException(status_code=400, detail="invalid task group")
    cache_variant = f"group:{group or ''}:category:{category or ''}"
    cached = cache_get("leaderboard", cache_variant)
    if cached is not None:
        cache_mark(response, hit=True)
        return cached
    cache_mark(response, hit=False)
    task_ids = _task_ids_for_group(db, group) if group else (
        [task["_id"] for task in db.tasks.find({"category": category}, {"_id": 1})] if category else None
    )
    rows = compute_leaderboard(db, task_ids=task_ids)
    judge = judge_summary(db, task_ids=task_ids)
    task_filter = {"$in": task_ids} if task_ids is not None else None

    score_query = {"is_deleted": {"$ne": True}}
    if task_filter is not None:
        score_query["task_id"] = task_filter
    votes = Counter(score["harness_key"] for score in db.scores.find(score_query, {"harness_key": 1}))

    durations: dict[str, list[float]] = defaultdict(list)
    run_query: dict = {"status": "done", "is_deleted": {"$ne": True}, "started_at": {"$ne": None}, "finished_at": {"$ne": None}}
    if task_filter is not None:
        run_query["task_id"] = task_filter
    for run in db.runs.find(run_query, {"harness_key": 1, "started_at": 1, "finished_at": 1}):
        seconds = (run["finished_at"] - run["started_at"]).total_seconds()
        if seconds >= 0:
            durations[run["harness_key"]].append(seconds)

    out = []
    for r in rows:
        j = judge.get(r["harness_key"]) or {}
        values = durations[r["harness_key"]]
        out.append(
            LeaderboardRow(
                **r,
                judge_mean=j.get("mean_score"),
                judge_graded=j.get("graded", 0),
                votes=votes[r["harness_key"]],
                median_time_seconds=median(values) if values else None,
            )
        )
    cache_set("leaderboard", out, variant=cache_variant, ttl_seconds=45)
    return out


@router.get("/harness/{harness_key}")
def get_harness_profile(harness_key: str, response: Response, db: Database = Depends(get_db)):
    cached = cache_get("leaderboard", f"harness:{harness_key}")
    if cached is not None:
        cache_mark(response, hit=True)
        return cached
    cache_mark(response, hit=False)
    adapter = all_adapters(db).get(harness_key)
    if adapter is None:
        raise HTTPException(status_code=404, detail="no harness answers to that id in the current roster")
    rows = compute_leaderboard(db)
    row = next((r for r in rows if r["harness_key"] == harness_key), None)
    out = {
        "key": harness_key,
        "name": adapter.name if adapter else harness_key,
        "tagline": adapter.tagline if adapter else "",
        "stats": row,
        # Both computed live from JudgeVerdict rows — nothing hardcoded.
        "judge": judge_summary(db, harness_key).get(harness_key),
        "judge_by_category": judge_summary_by_category(db, harness_key),
    }
    cache_set("leaderboard", out, variant=f"harness:{harness_key}", ttl_seconds=45)
    return out
