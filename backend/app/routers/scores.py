"""Blind compare + scoring.

`GET /compare/{task_id}` returns a task's completed runs labeled
"Output A/B/C..." in a freshly random order every single call (never seeded
by task_id or anything else derivable from the request — see the shuffle
below for why), so the judge never sees which harness produced which output
until *they* submit a verdict, and which run is "Output A" is never a
stable fact anyone could learn or reproduce across page loads. Every
signed-in person can judge a task once.

`POST /` accepts scores keyed by run_id (never harness_key — the client
only ever knows run ids from the anonymized compare listing), upserts one
score document per (task_id, harness_key), and requires every shown
response to be scored in the same submission (mirrors "score all outputs"
gating in the UI).

`include_community_stats=true` on GET /compare adds each entry's aggregate
score across every user who has judged it (not just the caller), for Battle
Log's non-blind dashboard view — see CompareEntry.community_avg_score.

`POST /reset/{task_id}` (admin only) wipes every human score for a task so
judging starts over from zero votes, without touching the runs/deliverables
those scores were about.
"""
from __future__ import annotations

import datetime as dt
import json
import random
import string

from fastapi import APIRouter, Depends, HTTPException
from pymongo.database import Database

from ..cache import invalidate as cache_invalidate
from ..db import get_db, next_id
from ..harnesses.registry import all_adapters
from ..logger import log_activity
from ..runner import latest_runs_by_harness
from ..schemas import CompareEntry, CompareOut, DeliverableOut, JudgeCriterionOut, ScoreIn, ScoreOut
from ..users import current_user, require_arena_admin, require_user
from ..webproject import is_web_project, partition_deliverables

# Sentinel score key standing in for every deliverable inside a web-project
# run's deployed frontend root — see webproject.partition_deliverables. Not
# a real deliverable id, so it can never collide with one (those are
# Mongo-assigned ints, always positive, never this string).
WEBSITE_SCORE_KEY = "website"

router = APIRouter(prefix="/api/scores", tags=["scores"])


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _labels(n: int) -> list[str]:
    # "Output A/B/C…" — matches the wording the judging UI shows, so an API
    # consumer and the interface never disagree about what a slot is called.
    return [f"Output {c}" for c in string.ascii_uppercase[:n]]


def _deliverables_for(run_id: int, db: Database) -> list[DeliverableOut]:
    return [
        DeliverableOut(id=d["_id"], filename=d["filename"], media_type=d["media_type"], size_bytes=d["size_bytes"], relpath=d.get("relpath", ""))
        for d in db.deliverables.find({"run_id": run_id}, {"content": 0})
    ]


def package_json_bytes_by_run(run_ids: list[int], db: Database) -> dict[int, dict[str, bytes]]:
    """{run_id: {relpath: content}} for every `package.json` among these
    runs' deliverables — the only content `partition_deliverables` ever
    reads (see `_package_json_score`). One `$in` query across every run
    requested, not one per run, so the bulk overview endpoint (many runs
    across many tasks) doesn't reintroduce the N+1 it exists to avoid."""
    out: dict[int, dict[str, bytes]] = {}
    if not run_ids:
        return out
    for d in db.deliverables.find(
        {"run_id": {"$in": run_ids}, "filename": "package.json"}, {"run_id": 1, "content": 1, "relpath": 1}
    ):
        if d.get("content") is None:
            continue
        out.setdefault(d["run_id"], {})[d.get("relpath") or "package.json"] = bytes(d["content"])
    return out


def website_and_extra_ids(
    deliverables: list[DeliverableOut], package_json_bytes: dict[str, bytes], expected_deliverables: str
) -> tuple[set[int], set[int]]:
    dicts = [{"id": d.id, "relpath": d.relpath, "filename": d.filename} for d in deliverables]
    return partition_deliverables(dicts, package_json_bytes, expected_deliverables)


def build_compare_entries(
    runs: list[dict],
    existing_scores: dict[str, dict],
    verdicts: dict[str, dict],
    community_stats: dict[str, tuple[float, int]],
    revealed: bool,
    adapters: dict,
    deliverables_for,
    website_ids_for=None,
) -> list[CompareEntry]:
    """The actual "Output A/B/C" labeling + entry assembly, factored out of
    `compare()` below so `routers/runs.py`'s bulk overview endpoint can
    build the exact same shape for many tasks in one request without
    re-implementing this logic a second time (and risking it drifting out
    of sync with the single-task endpoint). `deliverables_for(run_id)` is a
    callable rather than always querying Mongo directly, so the bulk path
    can pass a lookup into an already-bulk-fetched dict instead of one
    query per run. `website_ids_for(run_id)` is the same idea for the
    (website_ids, extra_ids) pair (see webproject.partition_deliverables);
    omitted entirely (rather than required) so a caller that hasn't been
    updated for it still gets the old "score every deliverable" shape —
    both id lists just stay empty, which is exactly what that means."""
    order = list(runs)
    random.SystemRandom().shuffle(order)

    entries = []
    for run, label in zip(order, _labels(len(order))):
        website_ids, extra_ids = website_ids_for(run["_id"]) if website_ids_for else (set(), set())
        entry = CompareEntry(
            label=label,
            run_id=run["_id"],
            deliverables=deliverables_for(run["_id"]),
            website_deliverable_ids=sorted(website_ids),
            extra_deliverable_ids=sorted(extra_ids),
            model=run.get("model") or "DeepSeek V4 Flash",
        )
        stats = community_stats.get(run["harness_key"])
        if stats is not None:
            entry.community_avg_score, entry.community_vote_count = stats
        if revealed:
            adapter = adapters.get(run["harness_key"])
            entry.harness_key = run["harness_key"]
            entry.harness_name = adapter.name if adapter else run["harness_key"]
            score = existing_scores.get(run["harness_key"])
            entry.already_scored = score["value"] if score else None
            entry.deliverable_scores = score.get("deliverable_scores", {}) if score else {}

            verdict = verdicts.get(run["harness_key"])
            if verdict is not None:
                entry.judge_score = verdict.get("score")
                entry.judge_note = verdict.get("note", "")
                try:
                    entry.judge_breakdown = [JudgeCriterionOut(**c) for c in verdict.get("breakdown", [])]
                except (TypeError, ValueError):
                    entry.judge_breakdown = []
            else:
                entry.judge_note = "No Artificial Analysis judge verdict for this task yet."
        entries.append(entry)
    return entries


def _selected_done_runs(db: Database, task_id: str, provider_config_id: int | None, run_ids: list[int] | None) -> list[dict]:
    """Return the exact requested completed runs, or the normal latest set.

    A Battle Log row is a snapshot of a specific comparison. Without this,
    following its link after a regeneration can swap in a different run and
    its unrelated deliverables under the same task URL.
    """
    if not run_ids:
        return list(latest_runs_by_harness(db, task_id, status="done", provider_config_id=provider_config_id).values())
    if len(set(run_ids)) != len(run_ids):
        raise HTTPException(status_code=400, detail="run ids must be unique")
    runs = list(
        db.runs.find(
            {
                "_id": {"$in": run_ids},
                "task_id": task_id,
                "status": "done",
                "is_deleted": {"$ne": True},
            }
        )
    )
    if len(runs) != len(run_ids):
        raise HTTPException(status_code=404, detail="one or more selected completed runs no longer exist")
    if len({run["harness_key"] for run in runs}) != len(runs):
        raise HTTPException(status_code=400, detail="select one completed run per harness")
    return runs


def _ensure_task_is_finished(db: Database, task_id: str) -> None:
    if db.runs.find_one(
        {"task_id": task_id, "status": {"$in": ["pending", "running"]}, "is_deleted": {"$ne": True}}, {"_id": 1}
    ):
        raise HTTPException(
            status_code=409,
            detail="this task is still in progress. Wait for every run to finish before judging",
        )


@router.get("/next-unjudged")
def next_unjudged(db: Database = Depends(get_db), user: dict = Depends(require_user)):
    """The task the "Start judging" action should jump to: the first one
    that has completed outputs ready to review but no scores yet.

    Returns `{"task_id": None, ...}` with a reason when there's nothing to
    judge, so the UI can explain why (no dataset loaded / nothing run yet /
    everything already judged) instead of silently going nowhere.
    """
    tasks = list(db.tasks.find({"is_deleted": {"$ne": True}}).sort("_id", 1))
    if not tasks:
        return {"task_id": None, "reason": "no_tasks", "judged": 0, "ready": 0}

    scored_task_ids = {s["task_id"] for s in db.scores.find({"user_id": user["_id"], "is_deleted": {"$ne": True}}, {"task_id": 1})}
    ready = 0
    first_unjudged = None
    for task in tasks:
        has_done_run = any(r["status"] == "done" for r in latest_runs_by_harness(db, task["_id"]).values())
        if not has_done_run:
            continue
        ready += 1
        if task["_id"] not in scored_task_ids and first_unjudged is None:
            first_unjudged = task["_id"]

    if first_unjudged:
        return {"task_id": first_unjudged, "reason": "ok", "judged": len(scored_task_ids), "ready": ready}
    if ready == 0:
        return {"task_id": None, "reason": "nothing_run", "judged": len(scored_task_ids), "ready": 0}
    return {"task_id": None, "reason": "all_judged", "judged": len(scored_task_ids), "ready": ready}


@router.get("/compare/{task_id}", response_model=CompareOut)
def compare(
    task_id: str,
    provider_config_id: int | None = None,
    run_ids: str | None = None,
    include_community_stats: bool = False,
    db: Database = Depends(get_db),
    user: dict | None = Depends(current_user),
):
    task = db.tasks.find_one({"_id": task_id, "is_deleted": {"$ne": True}})
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    _ensure_task_is_finished(db, task_id)

    try:
        requested_run_ids = [int(value) for value in run_ids.split(",") if value] if run_ids else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="run ids must be numbers") from exc
    runs = sorted(_selected_done_runs(db, task_id, provider_config_id, requested_run_ids), key=lambda r: r["_id"])
    if not runs:
        return CompareOut(task_id=task_id, revealed=False, entries=[])

    existing_scores = (
        {s["harness_key"]: s for s in db.scores.find({"task_id": task_id, "user_id": user["_id"], "provider_config_id": provider_config_id, "is_deleted": {"$ne": True}})} if user else {}
    )
    revealed = len(existing_scores) > 0
    adapters = all_adapters(db) if revealed else {}
    # The AI judge's verdict is reference material shown alongside a
    # human's own — never before it exists, so it can't anchor the human's
    # judgment. Fetching this is therefore gated on `revealed` exactly like
    # harness identity is.
    verdicts = (
        {v["harness_key"]: v for v in db.judge_verdicts.find({"task_id": task_id, "is_deleted": {"$ne": True}})} if revealed else {}
    )

    # Every user's score for this harness+profile, not just the caller's own
    # — deliberately independent of `revealed` (see CompareEntry's field
    # docstring for why that's safe): Battle Log wants "N people rated this
    # X on average" even for a viewer who hasn't personally judged it yet.
    community_stats: dict[str, tuple[float, int]] = {}
    if include_community_stats:
        by_harness: dict[str, list[float]] = {}
        for s in db.scores.find(
            {"task_id": task_id, "provider_config_id": provider_config_id, "is_deleted": {"$ne": True}},
            {"harness_key": 1, "value": 1},
        ):
            by_harness.setdefault(s["harness_key"], []).append(s["value"])
        community_stats = {hk: (round(sum(vals) / len(vals), 2), len(vals)) for hk, vals in by_harness.items()}

    # Precomputed once per run (rather than inside a closure that would
    # re-query per call) so deliverables_for and website_ids_for below
    # share the same fetch instead of hitting Mongo twice per run.
    deliverables_by_run = {run["_id"]: _deliverables_for(run["_id"], db) for run in runs}
    expected_deliverables = task.get("expected_deliverables", "")
    # Skip the package.json lookup query entirely for the overwhelming
    # majority of tasks (not web projects) — every /compare/{task_id} call
    # was paying for it unconditionally, which is the dominant regression
    # on ordinary judging page loads (see routers/runs.py's bulk overview
    # for the far worse version of the same mistake).
    package_json_by_run = package_json_bytes_by_run(list(deliverables_by_run), db) if is_web_project(expected_deliverables) else {}

    # Shuffling itself (NOT seeded by task_id — task_id is public, right
    # there in the URL, and a deterministic seed would make the "blind"
    # order computable by anyone) lives in build_compare_entries, along
    # with the actual entry assembly — shared with routers/runs.py's bulk
    # overview endpoint so both paths build "Output A/B/C" the exact same
    # way.
    entries = build_compare_entries(
        runs,
        existing_scores,
        verdicts,
        community_stats,
        revealed,
        adapters,
        lambda run_id: deliverables_by_run.get(run_id, []),
        lambda run_id: website_and_extra_ids(
            deliverables_by_run.get(run_id, []), package_json_by_run.get(run_id, {}), expected_deliverables
        ),
    )

    return CompareOut(task_id=task_id, revealed=revealed, entries=entries)


@router.post("", response_model=list[ScoreOut])
def submit_scores(body: ScoreIn, db: Database = Depends(get_db), _user=Depends(require_user)):
    task = db.tasks.find_one({"_id": body.task_id, "is_deleted": {"$ne": True}})
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    _ensure_task_is_finished(db, body.task_id)

    # Each user gets one blind verdict per task. Submitting reveals which
    # harness produced which output to that person, so a later score from the
    # same account would no longer be blind. Other users remain eligible.
    if db.scores.find_one({"task_id": body.task_id, "user_id": _user["_id"], "provider_config_id": body.provider_config_id, "is_deleted": {"$ne": True}}) is not None:
        raise HTTPException(
            status_code=409,
            detail="you have already judged this task. Identities are revealed, so you can't re-score it",
        )

    done_runs = _selected_done_runs(db, body.task_id, body.provider_config_id, body.run_ids)
    # One output has nothing to compare against — a "judgement" of just one
    # harness would still get stored and would still show up as a
    # community "vote" on that run (see routers/runs.py's runs_overview),
    # but compute_leaderboard skips any judgement covering fewer than two
    # harnesses outright (`if len(harnesses) < 2: continue`). Rejecting it
    # here instead of letting it through closes that gap at the source,
    # rather than leaving a vote that's real everywhere except the one
    # place ratings actually come from.
    if len({r["harness_key"] for r in done_runs}) < 2:
        raise HTTPException(
            status_code=400,
            detail="at least two completed outputs are needed to judge this task",
        )
    done_run_ids = {r["_id"] for r in done_runs}

    try:
        submitted_run_ids = {int(k) for k in body.scores.keys()}
    except ValueError:
        raise HTTPException(status_code=400, detail="score keys must be run ids")
    if submitted_run_ids != done_run_ids:
        raise HTTPException(
            status_code=400,
            detail="every completed response for this task must be scored in one submission",
        )

    expected_deliverables = task.get("expected_deliverables", "")
    # Same skip as compare() above — a submission for a non-web task has
    # no reason to pay for a package.json lookup that partition_deliverables
    # would immediately discard anyway.
    package_json_by_run = (
        package_json_bytes_by_run([r["_id"] for r in done_runs], db) if is_web_project(expected_deliverables) else {}
    )

    out = []
    now = _utcnow()
    for run in done_runs:
        deliverables = _deliverables_for(run["_id"], db)
        submitted = body.scores[str(run["_id"])]
        website_ids, extra_ids = website_and_extra_ids(deliverables, package_json_by_run.get(run["_id"], {}), expected_deliverables)
        # A web-project run collapses every deliverable inside its deployed
        # frontend root into the one WEBSITE_SCORE_KEY (see
        # build_compare_entries) — the judge scores the running site once,
        # not each of its source files. `extra_ids` (outside that root but
        # still named in the task's own expected deliverables) keep the
        # ordinary one-key-per-file requirement. An empty `website_ids`
        # means this isn't a web project (or has none pulled yet), so
        # nothing changes from the legacy "score every deliverable" shape.
        if website_ids:
            expected_keys = {WEBSITE_SCORE_KEY} | {str(i) for i in extra_ids}
        else:
            expected_keys = {str(d.id) for d in deliverables}
        if set(submitted) != expected_keys:
            raise HTTPException(status_code=400, detail="every deliverable must receive one score")
        if any(not (1 <= value <= 10) for value in submitted.values()):
            raise HTTPException(status_code=400, detail="scores must be between 1 and 10")
        value = round(sum(submitted.values()) / len(submitted), 2) if submitted else 0

        existing = db.scores.find_one(
            {"task_id": body.task_id, "harness_key": run["harness_key"], "user_id": _user["_id"], "provider_config_id": body.provider_config_id}
        )
        if existing is None:
            score_id = next_id(db, "scores")
            doc = {
                "_id": score_id,
                "task_id": body.task_id,
                "harness_key": run["harness_key"],
                "user_id": _user["_id"],
                "provider_config_id": body.provider_config_id,
                "run_id": run["_id"],
                "value": value,
                "deliverable_scores": submitted,
                "judged_at": now,
            }
            db.scores.insert_one(doc)
        else:
            doc = {**existing, "run_id": run["_id"], "value": value, "deliverable_scores": submitted, "judged_at": now}
            db.scores.update_one({"_id": existing["_id"]}, {"$set": {"run_id": run["_id"], "value": value, "deliverable_scores": submitted, "judged_at": now, "is_deleted": False}})
        out.append(doc)

    cache_invalidate("stats", "leaderboard", "runs_board")
    log_activity(
        db,
        action="SCORE_SUBMIT",
        user_id=_user["_id"],
        message=f"submitted blind judgement for task {body.task_id} across {len(out)} harness(es)",
        metadata={
            "task_id": body.task_id,
            "provider_config_id": body.provider_config_id,
            # Which harness got which score is the whole point of the audit
            # trail here; the scores themselves are the user's own verdict,
            # not a secret.
            "scores": {s["harness_key"]: s["value"] for s in out},
        },
        route="/api/scores",
    )
    return [ScoreOut(task_id=s["task_id"], harness_key=s["harness_key"], provider_config_id=s.get("provider_config_id"), value=s["value"], judged_at=s["judged_at"]) for s in out]


@router.post("/reset/{task_id}", status_code=204)
def reset_scores(task_id: str, db: Database = Depends(get_db), admin: dict = Depends(require_arena_admin)):
    """Wipe every human score for this task — every user, every profile —
    so judging starts over from zero votes. Deliberately narrower than
    "Delete results" (tasks.py): the runs and deliverables themselves are
    untouched, only who-scored-what-how-much. Also has the side effect of
    un-revealing the task for everyone (compare()'s `revealed` is just "does
    a score exist for this viewer"), which is the correct behavior — if
    nobody's blind verdict is on record any more, nobody should see
    identities either. A hard delete, not a soft one: unlike task/results
    deletion there's no "Restore" for this, previous scores are gone."""
    if db.tasks.find_one({"_id": task_id}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="task not found")
    deleted = db.scores.delete_many({"task_id": task_id}).deleted_count
    cache_invalidate("stats", "leaderboard", "runs_board")
    # A hard delete with no Restore (see this endpoint's docstring), so the
    # log entry is the only remaining record that it happened.
    log_activity(
        db,
        action="SCORES_RESET",
        user_id=admin["_id"],
        message=f"hard-deleted every human score for task {task_id} ({deleted} score row(s))",
        metadata={"task_id": task_id, "deleted_count": deleted},
        route="/api/scores/reset/{task_id}",
    )
