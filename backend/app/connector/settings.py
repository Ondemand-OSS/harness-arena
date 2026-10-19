from __future__ import annotations

import os

from .. import mongo as _mongo  # noqa: F401


def _text(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _csv(name: str, default: str) -> list[str]:
    return [item.strip() for item in _text(name, default).split(",") if item.strip()]


def _int(name: str, default: int) -> int:
    value = _text(name)
    return int(value) if value.lstrip("-").isdigit() else default


CLIENT_ID = _text("CONNECTOR_CLIENT_ID")
CLIENT_SECRET = os.environ.get("CONNECTOR_CLIENT_SECRET") or ""
REDIRECT_URIS = _csv("CONNECTOR_REDIRECT_URIS", "")
SCOPES = _csv("CONNECTOR_SCOPES", "chat,files")
ENABLED_AGENTS = _csv("CONNECTOR_ENABLED_AGENTS", "openclaw,hermes")
PUBLIC_BASE_URL = _text("CONNECTOR_PUBLIC_BASE_URL").rstrip("/")
_provider_config_id = _text("CONNECTOR_PROVIDER_CONFIG_ID")
PROVIDER_CONFIG_ID = int(_provider_config_id) if _provider_config_id.isdigit() else None

MAX_CONCURRENT_TASKS = max(1, _int("CONNECTOR_MAX_CONCURRENT_TASKS", 5))
MAX_ACTIVE_TASKS_PER_USER = max(1, _int("CONNECTOR_MAX_ACTIVE_TASKS_PER_USER", 3))
MAX_UPLOAD_BYTES = _int("CONNECTOR_MAX_UPLOAD_BYTES", 25 * 1024 * 1024)
MAX_FILES_PER_TASK = _int("CONNECTOR_MAX_FILES_PER_TASK", 20)
MAX_MESSAGE_CHARS = _int("CONNECTOR_MAX_MESSAGE_CHARS", 100000)
RETENTION_DAYS = max(1, _int("CONNECTOR_RETENTION_DAYS", 7))

ACCESS_TOKEN_TTL_SECONDS = _int("CONNECTOR_ACCESS_TOKEN_TTL_SECONDS", 3600)
REFRESH_TOKEN_TTL_SECONDS = _int("CONNECTOR_REFRESH_TOKEN_TTL_SECONDS", 30 * 24 * 3600)
CODE_TTL_SECONDS = 600
MIN_PASSWORD_LENGTH = 8
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCK_MINUTES = 15
