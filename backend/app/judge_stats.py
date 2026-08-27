"""Aggregates over the AI judge's verdicts — computed from the JudgeVerdict
rows actually in the database, never hardcoded.

These are deliberately separate from elo.py: that ranks harnesses by the
*human's* scores (the leaderboard), while this summarizes what the external
judge concluded. Both are shown, clearly labeled, and neither is derived
from the other.

Verdicts the source grading declined to score (`score is None` — e.g.
"invalid deliverables produced") are excluded from the mean rather than
counted as zero, and the count of graded-vs-total is reported alongside so
a mean over fewer tasks is never passed off as a complete one.
"""
from __future__ import annotations

from collections import defaultdict

from pymongo.database import Database

from .taxonomy import group_for_category


def _summarize(scores: list[float], total: int) -> dict:
    graded = len(scores)
    return {
        "mean_score": round(sum(scores) / graded, 2) if graded else None,
        "mean_score_pct": round((sum(scores) / graded) * 10, 1) if graded else None,
        "graded": graded,
        "total": total,
        "ungraded": total - graded,
    }


def judge_summary(
    db: Database, harness_key: str | None = None, task_ids: list[str] | None = None
) -> dict[str, dict]:
    """Per-harness judge summary: {harness_key: {mean_score, graded, ...}}."""
    query = {"is_deleted": {"$ne": True}}
    if harness_key:
        query["harness_key"] = harness_key
    if task_ids is not None:
        query["task_id"] = {"$in": task_ids}

    scores: dict[str, list[float]] = defaultdict(list)
    totals: dict[str, int] = defaultdict(int)
    for v in db.judge_verdicts.find(query):
        totals[v["harness_key"]] += 1
        if v.get("score") is not None:
            scores[v["harness_key"]].append(v["score"])

    return {key: _summarize(scores.get(key, []), totals[key]) for key in totals}


def judge_summary_by_category(db: Database, harness_key: str) -> list[dict]:
    """This harness's judge mean per task category, best first — the
    per-category breakdown shown on a harness profile."""
    categories = {t["_id"]: t["category"] for t in db.tasks.find({"is_deleted": {"$ne": True}}, {"category": 1})}

    scores: dict[str, list[float]] = defaultdict(list)
    totals: dict[str, int] = defaultdict(int)
    for v in db.judge_verdicts.find({"harness_key": harness_key, "is_deleted": {"$ne": True}}):
        category = categories.get(v["task_id"])
        if category is None:
            continue
        totals[category] += 1
        if v.get("score") is not None:
            scores[category].append(v["score"])

    rows = [
        {"category": category, "group": group_for_category(category), **_summarize(scores.get(category, []), total)}
        for category, total in totals.items()
    ]
    # Ungraded categories (mean_score None) sort last rather than crashing
    # the comparison or masquerading as a zero score.
    rows.sort(key=lambda r: (r["mean_score"] is None, -(r["mean_score"] or 0)))
    return rows
