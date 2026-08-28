"""Small, fail-open Upstash Redis response cache.

Only compact JSON derived from MongoDB belongs here. Deliverable bytes,
credentials, sessions, and user-specific responses are deliberately excluded.

Each namespace is one Redis hash. Query variants are hash fields, which lets a
single DEL invalidate every cached variant after a write without SCAN/KEYS and
without spending extra commands on every read. Values carry their own expiry;
the hash itself gets a longer safety TTL so abandoned fields cannot live
forever.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi.encoders import jsonable_encoder
from starlette.responses import Response

from .logger import get_logger

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
_PREFIX = os.environ.get("ARENA_CACHE_PREFIX", "harness-arena:v1").strip() or "harness-arena:v1"
_CLIENT = httpx.Client(timeout=httpx.Timeout(1.5, connect=1.0))
_log = get_logger("cache")


def enabled() -> bool:
    return bool(_URL and _TOKEN)


def mark_response(response: Response, *, hit: bool) -> None:
    """Expose cache provenance without changing an endpoint's JSON schema."""
    response.headers["X-Arena-Cache"] = "HIT" if hit else "MISS"
    response.headers["X-Arena-Data-Source"] = "redis" if hit else "mongodb"
    response.headers["X-Arena-Cache-Message"] = "response fetched from Redis" if hit else "response fetched from MongoDB"


def connection_status() -> str:
    """Used only by the explicit diagnostics endpoint, never on normal requests."""
    if not enabled():
        return "disabled"
    return "connected" if _command(["PING"]) == "PONG" else "unreachable"


def _key(namespace: str) -> str:
    return f"{_PREFIX}:{namespace}"


def _command(parts: list[Any]) -> Any:
    if not enabled():
        return None
    try:
        response = _CLIENT.post(
            _URL,
            headers={"Authorization": f"Bearer {_TOKEN}"},
            json=parts,
        )
        response.raise_for_status()
        return response.json().get("result")
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        # Redis is an optimization, never a dependency for correctness — but
        # a failed DEL means whatever it was invalidating stays live in the
        # cache until its TTL lapses, so this must be visible somewhere
        # (stdout/docker logs) instead of vanishing silently.
        _log.warning("redis command failed, cmd=%s: %s", parts[0] if parts else "?", exc)
        return None


def get_json(namespace: str, variant: str = "default") -> Any | None:
    raw = _command(["HGET", _key(namespace), variant])
    if not raw:
        return None
    try:
        envelope = json.loads(raw)
        if float(envelope["expires_at"]) <= time.time():
            return None
        return envelope["value"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def set_json(namespace: str, value: Any, *, variant: str = "default", ttl_seconds: int = 60) -> None:
    if not enabled():
        return
    envelope = json.dumps(
        {
            "expires_at": time.time() + ttl_seconds,
            "value": jsonable_encoder(value),
        },
        separators=(",", ":"),
    )
    key = _key(namespace)
    try:
        # One HTTP round trip. Upstash still counts the two Redis commands,
        # but writes happen only on misses and mutations are comparatively rare.
        response = _CLIENT.post(
            f"{_URL}/pipeline",
            headers={"Authorization": f"Bearer {_TOKEN}"},
            json=[
                ["HSET", key, variant, envelope],
                ["EXPIRE", key, max(ttl_seconds * 5, 300)],
            ],
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        _log.warning("redis set_json failed, namespace=%s variant=%s: %s", namespace, variant, exc)


def invalidate(*namespaces: str) -> None:
    """Bust every cached variant in each namespace (one DEL per namespace
    hash). Called right after a write, so a failure here isn't just a missed
    read-side optimization — it means the pre-write response for these
    namespaces keeps being served until its TTL lapses. `_command` logs the
    underlying failure; this only adds which namespaces were affected."""
    if not enabled():
        return
    keys = [_key(namespace) for namespace in dict.fromkeys(namespaces)]
    if keys and _command(["DEL", *keys]) is None:
        _log.warning("cache invalidation may have failed for namespaces=%s", list(dict.fromkeys(namespaces)))
