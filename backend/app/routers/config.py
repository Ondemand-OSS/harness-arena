"""Named model/provider profiles.

Several can be saved; which one a battle actually runs on is picked
explicitly per battle (task trigger, batch submit — see routers/runs.py,
routers/batches.py) via provider_config_id, not by some single
arena-wide "active" profile. Holding the model constant across harnesses
within one battle is the premise of the whole comparison, so a single run
can never mix profiles — but there's no standing default to manage
separately from that per-battle choice, so this router has no concept of
activation.

Setup endpoints require a signed-in user. Responses never include API keys.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from pymongo.database import Database

from ..cache import invalidate as cache_invalidate
from ..db import get_db, next_id
from ..logger import log_activity
from ..schemas import ProviderConfigIn, ProviderConfigOut
from ..users import is_admin, require_arena_admin, require_user

router = APIRouter(prefix="/api/config", tags=["config"])

VALID_REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
# Free (admin-funded) profiles default to low effort when the admin hasn't
# set one explicitly — cheap-by-default for the shared, free-to-use tier.
# OnDemand's own mapped model still gets its reasoning effort set by the
# admin per-model (see ondemand_models.py), same as before.
DEFAULT_FREE_REASONING_EFFORT = "low"

# Fixed set of model families a free profile can belong to. The admin adds
# one or more exact model names under each family; the picker groups by
# family so a user just chooses "deepseek" (etc.) without needing to know
# the underlying provider model string.
FREE_MODEL_FAMILIES = {"deepseek", "kimi", "glm", "minimax", "qwen"}


def effective_reasoning_effort(stored: str, free: bool) -> str:
    stored = (stored or "").strip().lower()
    if stored:
        return stored
    return DEFAULT_FREE_REASONING_EFFORT if free else ""


def _resolved_family(body: "ProviderConfigIn", free: bool) -> str:
    """Required for a free profile (must be one of FREE_MODEL_FAMILIES);
    ignored for personal profiles, which have no family grouping."""
    value = (body.family or "").strip().lower()
    if not free:
        return ""
    if value not in FREE_MODEL_FAMILIES:
        raise HTTPException(
            status_code=400,
            detail=f"family must be one of {sorted(FREE_MODEL_FAMILIES)}",
        )
    return value


def _resolved_ondemand_model_id(db: Database, body: "ProviderConfigIn", free: bool) -> int | None:
    """The admin-preset OnDemand model this free profile maps to (see
    routers/runs.py's require_ondemand_selection). Optional — a free
    profile without a mapping simply can't be paired with OnDemand in a
    battle yet."""
    if not free or body.ondemand_model_id is None:
        return None
    if db.ondemand_models.find_one({"_id": body.ondemand_model_id}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="OnDemand model not found")
    return body.ondemand_model_id


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _out(doc: dict, user: dict) -> ProviderConfigOut:
    # `is_shared` is a legacy storage field. Public visibility now means one
    # thing only: it is an OnDemand-funded free model.
    free = bool(doc.get("is_free") or doc.get("is_shared"))
    return ProviderConfigOut(
        id=doc["_id"],
        name=doc.get("name") or doc.get("model") or f"profile {doc['_id']}",
        model=doc.get("model", ""),
        base_url=doc.get("base_url", ""),
        has_api_key=bool(doc.get("api_key")),  # never echo the key itself
        is_shared=free,
        is_free=free,
        is_owned_by_me=_is_owned_by_me(doc, user),
        updated_at=doc.get("updated_at"),
        reasoning_effort=effective_reasoning_effort(doc.get("reasoning_effort", ""), free) if is_admin(user) else "",
        family=doc.get("family", ""),
        # Which OnDemand model this maps to is admin-curated wiring, not
        # something a regular user's model picker needs to see or act on.
        ondemand_model_id=doc.get("ondemand_model_id") if is_admin(user) else None,
        # Missing on rows created before this field existed — treat those
        # as enabled so nothing already in use silently disappears.
        enabled=doc.get("enabled", True),
    )


def _is_owned_by_me(doc: dict, user: dict) -> bool:
    """Return whether this profile belongs to the caller.

    Profiles created before user accounts existed have no owner field. They
    are legacy OnDemand profiles, so the arena admin may manage them and the
    first edit assigns them to that admin account. Other users never see
    them as personal profiles.
    """
    owner_user_id = doc.get("owner_user_id")
    if owner_user_id is not None and str(owner_user_id) == str(user["_id"]):
        return True
    return (
        "owner_user_id" not in doc
        and not bool(doc.get("is_free") or doc.get("is_shared"))
        and is_admin(user)
    )


def _resolved_reasoning_effort(body: ProviderConfigIn, free: bool, user: dict) -> str:
    value = (body.reasoning_effort or "").strip().lower()
    if not value or not free or not is_admin(user):
        return ""
    if value not in VALID_REASONING_EFFORTS:
        raise HTTPException(
            status_code=400,
            detail=f"reasoning_effort must be one of {sorted(VALID_REASONING_EFFORTS)}",
        )
    return value


@router.get("", response_model=list[ProviderConfigOut])
def list_configs(db: Database = Depends(get_db), user: dict = Depends(require_user)):
    clauses = [{"is_shared": True}, {"owner_user_id": user["_id"]}, {"owner_user_id": str(user["_id"])}]
    # Legacy profiles existed before accounts. Only the OnDemand admin can
    # see and claim these private profiles; they must not appear as another
    # user's personal configuration.
    if is_admin(user):
        clauses.append({"owner_user_id": {"$exists": False}})
    query = {"$or": clauses}
    if not is_admin(user):
        # A disabled free profile still exists (so old runs still resolve
        # it, and the admin can flip it back on) — it's just hidden from
        # everyone else's model picker. The admin still sees it, with the
        # toggle to turn it back on.
        query = {"$and": [query, {"enabled": {"$ne": False}}]}
    return [_out(c, user) for c in db.provider_config.find(query).sort("_id", 1)]


@router.post("", response_model=ProviderConfigOut)
def create_config(body: ProviderConfigIn, db: Database = Depends(get_db), user: dict = Depends(require_user)):
    free = bool(body.is_free)
    if free and not is_admin(user):
        raise HTTPException(status_code=403, detail="only the OnDemand admin can create free profiles")
    # Personal (bring-your-own-key) provider profiles are temporarily
    # disabled — the arena now runs on admin-funded free models only, each
    # user picks from those instead of adding their own OpenRouter key.
    # Restore the block below (and drop this one) to bring BYOK back.
    if not free:
        raise HTTPException(
            status_code=403,
            detail="Personal provider profiles are temporarily disabled — pick one of the admin's free models instead.",
        )
    # if not free and not body.api_key:
    #     raise HTTPException(status_code=400, detail="a personal provider profile needs an API key")
    config_id = next_id(db, "provider_config")
    now = _utcnow()
    doc = {
        "_id": config_id,
        "name": body.name,
        "model": body.model,
        "base_url": body.base_url,
        "api_key": body.api_key,
        "is_shared": free,  # kept for backwards-compatible Mongo queries
        "is_free": free,
        "owner_user_id": None if free else user["_id"],
        "reasoning_effort": _resolved_reasoning_effort(body, free, user),
        "family": _resolved_family(body, free),
        "ondemand_model_id": _resolved_ondemand_model_id(db, body, free),
        "enabled": bool(body.enabled),
        "updated_at": now,
    }
    db.provider_config.insert_one(doc)
    cache_invalidate("stats")
    # api_key deliberately never goes into the log payload — only whether
    # one was supplied (see logger.py's "no secrets" contract).
    log_activity(
        db,
        action="PROVIDER_CONFIG_CREATE",
        user_id=user["_id"],
        message=f"created provider profile {config_id} ({body.name})",
        metadata={
            "config_id": config_id,
            "name": body.name,
            "model": body.model,
            "base_url": body.base_url,
            "is_free": free,
            "has_api_key": bool(body.api_key),
        },
        route="/api/config",
    )
    return _out(doc, user)


@router.put("/{config_id}", response_model=ProviderConfigOut)
def update_config(config_id: int, body: ProviderConfigIn, db: Database = Depends(get_db), user: dict = Depends(require_user)):
    doc = db.provider_config.find_one({"_id": config_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="config not found")
    owns = _is_owned_by_me(doc, user)
    was_free = bool(doc.get("is_free") or doc.get("is_shared"))
    if was_free:
        if not is_admin(user):
            raise HTTPException(status_code=403, detail="only the OnDemand admin can edit free profiles")
    elif not owns:
        raise HTTPException(status_code=403, detail="you can only edit your own provider profiles")
    free = bool(body.is_free)
    if free and not is_admin(user):
        raise HTTPException(status_code=403, detail="only the OnDemand admin can create free profiles")
    # Personal (bring-your-own-key) provider profiles are temporarily
    # disabled — see the matching block in create_config above for why.
    if not free:
        raise HTTPException(
            status_code=403,
            detail="Personal provider profiles are temporarily disabled — pick one of the admin's free models instead.",
        )
    # if not free and not body.api_key and not doc.get("api_key"):
    #     raise HTTPException(status_code=400, detail="a private provider profile needs an API key")
    update = {
        "name": body.name,
        "model": body.model,
        "base_url": body.base_url,
        "is_free": free,
        "is_shared": free,
        "owner_user_id": None if free else user["_id"],
        "reasoning_effort": _resolved_reasoning_effort(body, free, user),
        "family": _resolved_family(body, free),
        "ondemand_model_id": _resolved_ondemand_model_id(db, body, free),
        "updated_at": _utcnow(),
    }
    if body.api_key:  # blank means "keep the stored key"
        update["api_key"] = body.api_key
    # `enabled` is deliberately NOT part of a regular edit: ProviderConfigIn
    # defaults it to True, so folding body.enabled in here would silently
    # re-enable a disabled profile the moment its name/model/etc. is edited
    # for anything else. It's only ever changed via toggle_config_enabled
    # below.
    db.provider_config.update_one({"_id": config_id}, {"$set": update})
    cache_invalidate("stats")
    log_activity(
        db,
        action="PROVIDER_CONFIG_UPDATE",
        user_id=user["_id"],
        message=f"updated provider profile {config_id} ({body.name})",
        metadata={
            "config_id": config_id,
            "name": body.name,
            "model": body.model,
            "base_url": body.base_url,
            "is_free": free,
            # Whether the stored key was replaced this edit — never the key.
            "api_key_replaced": bool(body.api_key),
        },
        route="/api/config/{config_id}",
    )
    return _out({**doc, **update}, user)


class ProviderConfigEnabledIn(BaseModel):
    enabled: bool


@router.put("/{config_id}/enabled", response_model=ProviderConfigOut)
def toggle_config_enabled(
    config_id: int, body: ProviderConfigEnabledIn, db: Database = Depends(get_db), admin_user: dict = Depends(require_arena_admin)
):
    """Admin on/off switch for a free profile — the model picker filter
    (list_configs above) is the only place this actually takes effect;
    turning a profile off doesn't touch any run that already used it."""
    doc = db.provider_config.find_one({"_id": config_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="config not found")
    db.provider_config.update_one({"_id": config_id}, {"$set": {"enabled": body.enabled}})
    cache_invalidate("stats")
    log_activity(
        db,
        action="PROVIDER_CONFIG_ENABLED_TOGGLE",
        user_id=admin_user["_id"],
        message=f"{'enabled' if body.enabled else 'disabled'} provider profile {config_id} ({doc.get('name')})",
        metadata={"config_id": config_id, "name": doc.get("name"), "enabled": body.enabled},
        route="/api/config/{config_id}/enabled",
    )
    return _out({**doc, "enabled": body.enabled}, admin_user)


@router.delete("/{config_id}", status_code=204)
def delete_config(config_id: int, db: Database = Depends(get_db), user: dict = Depends(require_user)):
    doc = db.provider_config.find_one({"_id": config_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="config not found")
    if doc.get("is_free") or doc.get("is_shared", False):
        if not is_admin(user):
            raise HTTPException(status_code=403, detail="only the OnDemand admin can remove free profiles")
    elif not _is_owned_by_me(doc, user):
        raise HTTPException(status_code=403, detail="you can only remove your own provider profiles")
    db.provider_config.delete_one({"_id": config_id})
    log_activity(
        db,
        action="PROVIDER_CONFIG_DELETE",
        user_id=user["_id"],
        message=f"deleted provider profile {config_id} ({doc.get('name')})",
        metadata={"config_id": config_id, "name": doc.get("name"), "model": doc.get("model"), "is_free": bool(doc.get("is_free") or doc.get("is_shared"))},
        route="/api/config/{config_id}",
    )
    cache_invalidate("stats")
