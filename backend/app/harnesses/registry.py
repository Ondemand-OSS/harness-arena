"""Declarative catalog of harnesses the arena knows about.

Two sources are merged:
- BUILTIN: harnesses defined in Python (real `claude`/`codex` CLI adapters,
  and OnDemand's own hosted API).
- Custom (bring-your-own-harness): documents in the `custom_harnesses`
  collection, added from the Setup page with no code change or redeploy,
  each turned into a WebhookAdapter on the fly.

A builtin key always wins if it collides with a custom one (defensive only
— the Setup UI should never let you register a colliding key).
"""
from __future__ import annotations

from pymongo.database import Database

from .base import HarnessAdapter
from .claude_code import ClaudeCodeAdapter
from .codex_cli import CodexAdapter
from .hermes_cli import HermesAdapter
from .ondemand import OnDemandAdapter
from .openclaw_cli import OpenClawAdapter
from .opencode_cli import OpenCodeAdapter
from .webhook import WebhookAdapter

BUILTIN: dict[str, HarnessAdapter] = {
    "claude-code": ClaudeCodeAdapter(),
    "codex": CodexAdapter(),
    "ondemand": OnDemandAdapter(),
    "hermes": HermesAdapter(),
    "openclaw": OpenClawAdapter(),
    "opencode": OpenCodeAdapter(),
}


def _custom_adapter(doc: dict) -> WebhookAdapter:
    return WebhookAdapter(
        key=doc["_id"],
        name=doc["name"],
        tagline=doc.get("tagline", ""),
        webhook_url=doc["webhook_url"],
        auth_header=doc.get("auth_header", "Authorization"),
        auth_token=doc.get("auth_token", ""),
    )


def all_adapters(db: Database) -> dict[str, HarnessAdapter]:
    merged = dict(BUILTIN)
    for doc in db.custom_harnesses.find():
        if doc["_id"] not in merged:  # builtin keys always win
            merged[doc["_id"]] = _custom_adapter(doc)
    return merged


def enabled_harness_keys(db: Database) -> list[str]:
    keys = [key for key, adapter in BUILTIN.items() if getattr(adapter, "enabled", True)]
    keys += [doc["_id"] for doc in db.custom_harnesses.find({"enabled": True})]
    return keys


def get_adapter(db: Database, key: str) -> HarnessAdapter:
    if key in BUILTIN:
        return BUILTIN[key]

    doc = db.custom_harnesses.find_one({"_id": key})
    if doc is None:
        raise KeyError(f"unknown harness key: {key}")
    return _custom_adapter(doc)
