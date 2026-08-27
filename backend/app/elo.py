"""Derived Elo leaderboard.

Ratings are recomputed from the full score history whenever the leaderboard
is read, preventing stored aggregates from drifting from the source data.

Equal weight per submitted judgement: a task where 2 harnesses were scored and a task
where 4 harnesses were scored must move the leaderboard by the same total
magnitude. We do this by, for each task, running every pairwise comparison
among that task's scored harnesses off of a SHARED pre-task rating snapshot
(so within-task pair order doesn't matter) and scaling each pair's K by
1 / num_pairs, so the total rating mass exchanged in one task is capped at
BASE_K regardless of how many harnesses it compared.
"""
from __future__ import annotations

import itertools
from collections import defaultdict

from pymongo.database import Database

BASE_RATING = 1000.0
BASE_K = 32.0


def _expected(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def compute_leaderboard(
    db: Database, category: str | None = None, task_ids: list[str] | None = None
) -> list[dict]:
    query: dict = {}
    if task_ids is not None:
        query["task_id"] = {"$in": task_ids}
    elif category:
        task_ids = [t["_id"] for t in db.tasks.find({"category": category}, {"_id": 1})]
        query["task_id"] = {"$in": task_ids}
    query["is_deleted"] = {"$ne": True}
    scores = list(db.scores.find(query).sort("judged_at", 1))

    by_judgement: dict[tuple[str, int], dict[str, float]] = defaultdict(dict)
    judgement_order: list[tuple[str, int]] = []
    for s in scores:
        judgement = (s["task_id"], s.get("user_id", 0))
        if judgement not in by_judgement:
            judgement_order.append(judgement)
        by_judgement[judgement][s["harness_key"]] = s["value"]

    ratings: dict[str, float] = defaultdict(lambda: BASE_RATING)
    battles: dict[str, int] = defaultdict(int)
    wins: dict[str, float] = defaultdict(float)
    losses: dict[str, float] = defaultdict(float)
    ties: dict[str, float] = defaultdict(float)
    # Rating right after each judgement a harness took part in — the real
    # trajectory behind the leaderboard's sparkline, not synthesized noise.
    # A harness with only one point renders as a flat/absent trend, which is
    # honest: there's nothing to show a trend over yet.
    history: dict[str, list[float]] = defaultdict(list)
    participations: dict[str, int] = defaultdict(int)
    # Rank immediately before each harness's most recent judgement. This is
    # intentionally rank, not Elo points: the UI's “Move” column is about
    # places on the board.
    prior_rank: dict[str, int] = {}

    for judgement in judgement_order:
        task_scores = by_judgement[judgement]
        harnesses = sorted(task_scores.keys())
        if len(harnesses) < 2:
            continue  # nothing to compare yet for this task

        ranked_before = sorted(set(ratings) | set(harnesses), key=lambda h: (-ratings[h], h))
        ranks_before = {h: i + 1 for i, h in enumerate(ranked_before)}
        for h in harnesses:
            prior_rank[h] = ranks_before[h]
            participations[h] += 1

        pairs = list(itertools.combinations(harnesses, 2))
        k = BASE_K / len(pairs)
        snapshot = {h: ratings[h] for h in harnesses}
        deltas: dict[str, float] = defaultdict(float)

        for a, b in pairs:
            ra, rb = snapshot[a], snapshot[b]
            va, vb = task_scores[a], task_scores[b]
            if va > vb:
                actual_a = 1.0
            elif va < vb:
                actual_a = 0.0
            else:
                actual_a = 0.5

            exp_a = _expected(ra, rb)
            deltas[a] += k * (actual_a - exp_a)
            deltas[b] += k * ((1.0 - actual_a) - (1.0 - exp_a))

            battles[a] += 1
            battles[b] += 1
            if actual_a == 1.0:
                wins[a] += 1
                losses[b] += 1
            elif actual_a == 0.0:
                losses[a] += 1
                wins[b] += 1
            else:
                ties[a] += 1
                ties[b] += 1

        for h in harnesses:
            ratings[h] += deltas[h]
            history[h].append(round(ratings[h], 1))

    ranked_final = sorted(set(ratings) | set(battles), key=lambda h: (-ratings[h], h))
    final_rank = {h: i + 1 for i, h in enumerate(ranked_final)}
    rows = []
    all_harnesses = set(ratings.keys()) | set(battles.keys())
    for h in all_harnesses:
        b = battles[h]
        w = wins[h]
        rows.append(
            {
                "harness_key": h,
                "elo": round(ratings[h], 1),
                "battles": b,
                "wins": wins[h],
                "losses": losses[h],
                "ties": ties[h],
                "win_rate": round(w / b, 4) if b else 0.0,
                "history": history[h],
                "rank_movement": (prior_rank[h] - final_rank[h]) if participations[h] > 1 else None,
            }
        )
    rows.sort(key=lambda r: (-r["elo"], r["harness_key"]))
    return rows
