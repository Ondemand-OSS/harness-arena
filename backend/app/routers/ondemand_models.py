"""Admin-curated whitelist of OnDemand models.

OnDemand's own API (see harnesses/ondemand.py) only accepts specific
predefined `endpointId` strings, not free text — so unlike every other
harness's provider profile (config.py, free-typed model id), the set of
selectable OnDemand models is a small admin-curated list every signed-in
user picks from, never types themselves.

Each free provider profile (config.py) now carries its own
`ondemand_model_id` pointing at a row here, set by the admin — that preset
mapping is what a battle resolves OnDemand's model from (see
runner.resolve_ondemand_model_id / routers/runs.py's
require_ondemand_selection), rather than the caller picking one by hand and
having it fuzzy-matched against the shared profile's model string.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from pymongo.database import Database

from ..db import get_db, next_id
from ..logger import log_activity
from ..users import is_admin, require_arena_admin, require_user
from .config import VALID_REASONING_EFFORTS

router = APIRouter(prefix="/api/ondemand-models", tags=["ondemand-models"])


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class OndemandModelIn(BaseModel):
    label: str
    endpoint_id: str
    reasoning_effort: str = ""


class OndemandModelOut(BaseModel):
    id: int
    label: str
    endpoint_id: str
    # Populated only for an admin viewer — see `_out` below. A regular
    # user's model picker never needs or sees this.
    reasoning_effort: str = ""
    # Disabled models remain visible to the admin for management, but are
    # hidden from regular users and cannot start new OnDemand runs.
    enabled: bool = True


def _out(doc: dict, admin: bool) -> OndemandModelOut:
    return OndemandModelOut(
        id=doc["_id"],
        label=doc["label"],
        endpoint_id=doc["endpoint_id"],
        reasoning_effort=doc.get("reasoning_effort", "") if admin else "",
        enabled=doc.get("enabled", True),
    )


def _resolved_reasoning_effort(body: OndemandModelIn) -> str:
    value = (body.reasoning_effort or "").strip().lower()
    if not value:
        return ""
    if value not in VALID_REASONING_EFFORTS:
        raise HTTPException(
            status_code=400,
            detail=f"reasoning_effort must be one of {sorted(VALID_REASONING_EFFORTS)}",
        )
    return value


@router.get("", response_model=list[OndemandModelOut])
def list_ondemand_models(db: Database = Depends(get_db), user: dict = Depends(require_user)):
    """Every signed-in user can see the whitelist — only the admin can
    change it (enforced below), same split as config.py's shared profiles."""
    admin = is_admin(user)
    query = {} if admin else {"enabled": {"$ne": False}}
    return [_out(doc, admin) for doc in db.ondemand_models.find(query).sort("_id", 1)]


# Admin on/off switch for OnDemand's plugin-suggestion feature (see
# harnesses/ondemand.py's _suggest_plugin_ids) — stored in arena_settings
# (a flat _id-keyed collection; see users.py's session-key row for the same
# pattern) rather than here, since it's not one more curated model, it's a
# behavior toggle for the harness itself.
SUGGEST_PLUGINS_SETTING_ID = "ondemand_suggest_plugins_enabled"
# Off by default: suggestions have been unreliable, naming agents unrelated
# to the task that then turn out inactive on OnDemand's side.
SUGGEST_PLUGINS_DEFAULT_ENABLED = False


def suggest_plugins_enabled(db: Database) -> bool:
    doc = db.arena_settings.find_one({"_id": SUGGEST_PLUGINS_SETTING_ID})
    return bool(doc["value"]) if doc else SUGGEST_PLUGINS_DEFAULT_ENABLED


class SuggestPluginsSettingIO(BaseModel):
    enabled: bool


@router.get("/suggest-plugins", response_model=SuggestPluginsSettingIO)
def get_suggest_plugins_setting(db: Database = Depends(get_db), _admin: dict = Depends(require_arena_admin)):
    return SuggestPluginsSettingIO(enabled=suggest_plugins_enabled(db))


@router.put("/suggest-plugins", response_model=SuggestPluginsSettingIO)
def set_suggest_plugins_setting(
    body: SuggestPluginsSettingIO, db: Database = Depends(get_db), admin_user: dict = Depends(require_arena_admin)
):
    db.arena_settings.update_one({"_id": SUGGEST_PLUGINS_SETTING_ID}, {"$set": {"value": body.enabled}}, upsert=True)
    log_activity(
        db,
        action="ONDEMAND_SUGGEST_PLUGINS_TOGGLE",
        user_id=admin_user["_id"],
        message=f"{'enabled' if body.enabled else 'disabled'} OnDemand plugin suggestions",
        metadata={"enabled": body.enabled},
        route="/api/ondemand-models/suggest-plugins",
    )
    return SuggestPluginsSettingIO(enabled=body.enabled)


@router.post("", response_model=OndemandModelOut)
def create_ondemand_model(body: OndemandModelIn, db: Database = Depends(get_db), admin_user: dict = Depends(require_arena_admin)):
    model_id = next_id(db, "ondemand_models")
    doc = {
        "_id": model_id,
        "label": body.label.strip(),
        "endpoint_id": body.endpoint_id.strip(),
        "reasoning_effort": _resolved_reasoning_effort(body),
        "enabled": True,
        "created_at": _utcnow(),
    }
    db.ondemand_models.insert_one(doc)
    log_activity(
        db,
        action="ONDEMAND_MODEL_CREATE",
        user_id=admin_user["_id"],
        message=f"added OnDemand model {model_id} ({doc['label']})",
        metadata={"model_id": model_id, "label": doc["label"], "endpoint_id": doc["endpoint_id"]},
        route="/api/ondemand-models",
    )
    return _out(doc, admin=True)


@router.put("/{model_id}", response_model=OndemandModelOut)
def update_ondemand_model(
    model_id: int, body: OndemandModelIn, db: Database = Depends(get_db), admin_user: dict = Depends(require_arena_admin)
):
    doc = db.ondemand_models.find_one({"_id": model_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="OnDemand model not found")
    update = {
        "label": body.label.strip(),
        "endpoint_id": body.endpoint_id.strip(),
        "reasoning_effort": _resolved_reasoning_effort(body),
    }
    db.ondemand_models.update_one({"_id": model_id}, {"$set": update})
    log_activity(
        db,
        action="ONDEMAND_MODEL_UPDATE",
        user_id=admin_user["_id"],
        message=f"updated OnDemand model {model_id} ({update['label']})",
        metadata={"model_id": model_id, "label": update["label"], "endpoint_id": update["endpoint_id"]},
        route="/api/ondemand-models/{model_id}",
    )
    return _out({**doc, **update}, admin=True)


class EnabledIn(BaseModel):
    enabled: bool


@router.put("/{model_id}/enabled", response_model=OndemandModelOut)
def toggle_ondemand_model_enabled(
    model_id: int, body: EnabledIn, db: Database = Depends(get_db), admin_user: dict = Depends(require_arena_admin)
):
    doc = db.ondemand_models.find_one({"_id": model_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="OnDemand model not found")
    db.ondemand_models.update_one({"_id": model_id}, {"$set": {"enabled": body.enabled}})
    log_activity(
        db,
        action="ONDEMAND_MODEL_TOGGLE",
        user_id=admin_user["_id"],
        message=f"{'enabled' if body.enabled else 'disabled'} OnDemand model {model_id} ({doc.get('label')})",
        metadata={"model_id": model_id, "label": doc.get("label"), "enabled": body.enabled},
        route="/api/ondemand-models/{model_id}/enabled",
    )
    return _out({**doc, "enabled": body.enabled}, admin=True)


@router.delete("/{model_id}", status_code=204)
def delete_ondemand_model(model_id: int, db: Database = Depends(get_db), admin_user: dict = Depends(require_arena_admin)):
    doc = db.ondemand_models.find_one({"_id": model_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="OnDemand model not found")
    db.ondemand_models.delete_one({"_id": model_id})
    log_activity(
        db,
        action="ONDEMAND_MODEL_DELETE",
        user_id=admin_user["_id"],
        message=f"deleted OnDemand model {model_id} ({doc.get('label')})",
        metadata={"model_id": model_id, "label": doc.get("label"), "endpoint_id": doc.get("endpoint_id")},
        route="/api/ondemand-models/{model_id}",
    )
