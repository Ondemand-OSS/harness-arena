from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Response
from pymongo.database import Database

from ..cache import get_json as cache_get, invalidate as cache_invalidate, mark_response as cache_mark, set_json as cache_set
from ..db import get_db
from ..harnesses.registry import BUILTIN
from ..logger import log_activity
from ..schemas import CustomHarnessIn, CustomHarnessOut, HarnessBattleOut, HarnessInfo, TaskOut
from ..users import current_user, require_user

router = APIRouter(prefix="/api/harnesses", tags=["harnesses"])


def _custom_out(doc: dict) -> CustomHarnessOut:
    return CustomHarnessOut(
        key=doc["_id"],
        name=doc["name"],
        tagline=doc.get("tagline", ""),
        webhook_url=doc["webhook_url"],
        auth_header=doc.get("auth_header", "Authorization"),
        has_auth_token=bool(doc.get("auth_token")),
        enabled=doc.get("enabled", True),
    )


@router.get("", response_model=list[HarnessInfo])
def list_harnesses(response: Response, db: Database = Depends(get_db)):
    """Full roster: builtin harnesses (including disabled/coming-soon ones
    like OnDemand) plus every registered bring-your-own-harness."""
    cached = cache_get("harnesses")
    if cached is not None:
        cache_mark(response, hit=True)
        return cached
    cache_mark(response, hit=False)
    out = [
        HarnessInfo(
            key=adapter.key,
            name=adapter.name,
            tagline=adapter.tagline,
            enabled=getattr(adapter, "enabled", True),
            is_custom=False,
        )
        for adapter in BUILTIN.values()
    ]
    for doc in db.custom_harnesses.find():
        out.append(
            HarnessInfo(
                key=doc["_id"],
                name=doc["name"],
                tagline=doc.get("tagline", ""),
                enabled=doc.get("enabled", True),
                is_custom=True,
            )
        )
    cache_set("harnesses", out, ttl_seconds=120)
    return out


@router.get("/{key}/battles", response_model=list[HarnessBattleOut])
def harness_battles(key: str, db: Database = Depends(get_db), user: dict | None = Depends(current_user)):
    """Every revealed task this harness has a score in, with win/loss/tie.

    Replaces HarnessProfile.jsx's old N+1 (one GET /api/compare/{id} per
    task): reuses runs.py's bulk overview builder instead so this is a
    fixed handful of queries regardless of task count.
    """
    from .runs import _build_overviews
    from .tasks import list_tasks as _list_tasks_for_battles

    raw_tasks = _list_tasks_for_battles(Response(), lean=True, db=db, user=user)
    tasks = [t if isinstance(t, TaskOut) else TaskOut.model_validate(t) for t in raw_tasks]
    overviews = _build_overviews([t.id_aa for t in tasks], db, user)

    out = []
    for task in tasks:
        overview = overviews.get(task.id_aa)
        if overview is None or not overview.compare.revealed:
            continue
        mine = next((e for e in overview.compare.entries if e.harness_key == key), None)
        if mine is None or mine.already_scored is None:
            continue
        best = max(e.already_scored for e in overview.compare.entries if e.already_scored is not None)
        top_count = sum(1 for e in overview.compare.entries if e.already_scored == best)
        if mine.already_scored != best:
            result = "Loss"
        else:
            result = "Tie" if top_count > 1 else "Win"
        out.append(HarnessBattleOut(task=task, score=mine.already_scored, result=result))
    return out


@router.get("/custom", response_model=list[CustomHarnessOut], dependencies=[Depends(require_user)])
def list_custom_harnesses(db: Database = Depends(get_db)):
    return [_custom_out(doc) for doc in db.custom_harnesses.find()]


@router.post("/custom", response_model=CustomHarnessOut)
def create_custom_harness(body: CustomHarnessIn, db: Database = Depends(get_db), user: dict = Depends(require_user)):
    if body.key in BUILTIN:
        raise HTTPException(status_code=400, detail=f"'{body.key}' is a builtin harness key and can't be overridden")
    if db.custom_harnesses.find_one({"_id": body.key}) is not None:
        raise HTTPException(status_code=409, detail=f"a custom harness with key '{body.key}' already exists")
    doc = {
        "_id": body.key,
        "name": body.name,
        "tagline": body.tagline,
        "webhook_url": body.webhook_url,
        "auth_header": body.auth_header,
        "auth_token": body.auth_token,
        "enabled": body.enabled,
        "created_at": dt.datetime.now(dt.timezone.utc),
    }
    db.custom_harnesses.insert_one(doc)
    cache_invalidate("harnesses", "stats", "leaderboard", "runs_board")
    # auth_token is a credential — only whether one was set, never its value.
    log_activity(
        db,
        action="CUSTOM_HARNESS_CREATE",
        user_id=user["_id"],
        message=f"registered custom harness {body.key} ({body.name})",
        metadata={"key": body.key, "name": body.name, "webhook_url": body.webhook_url, "enabled": body.enabled, "has_auth_token": bool(body.auth_token)},
        route="/api/harnesses/custom",
    )
    return _custom_out(doc)


@router.put("/custom/{key}", response_model=CustomHarnessOut)
def update_custom_harness(key: str, body: CustomHarnessIn, db: Database = Depends(get_db), user: dict = Depends(require_user)):
    doc = db.custom_harnesses.find_one({"_id": key})
    if doc is None:
        raise HTTPException(status_code=404, detail="custom harness not found")
    update = {
        "name": body.name,
        "tagline": body.tagline,
        "webhook_url": body.webhook_url,
        "auth_header": body.auth_header,
        "enabled": body.enabled,
    }
    if body.auth_token:  # blank in the PUT body means "keep the existing token"
        update["auth_token"] = body.auth_token
    db.custom_harnesses.update_one({"_id": key}, {"$set": update})
    cache_invalidate("harnesses", "stats", "leaderboard", "runs_board")
    log_activity(
        db,
        action="CUSTOM_HARNESS_UPDATE",
        user_id=user["_id"],
        message=f"updated custom harness {key} ({body.name})",
        metadata={"key": key, "name": body.name, "webhook_url": body.webhook_url, "enabled": body.enabled, "auth_token_replaced": bool(body.auth_token)},
        route="/api/harnesses/custom/{key}",
    )
    return _custom_out({**doc, **update})


@router.delete("/custom/{key}", status_code=204)
def delete_custom_harness(key: str, db: Database = Depends(get_db), user: dict = Depends(require_user)):
    result = db.custom_harnesses.delete_one({"_id": key})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="custom harness not found")
    cache_invalidate("harnesses", "stats", "leaderboard", "runs_board")
    log_activity(
        db,
        action="CUSTOM_HARNESS_DELETE",
        user_id=user["_id"],
        message=f"deleted custom harness {key}",
        metadata={"key": key},
        route="/api/harnesses/custom/{key}",
    )
