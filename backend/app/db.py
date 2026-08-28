"""Database access compatibility exports."""

from __future__ import annotations

from .mongo import close_client, ensure_indexes as init_db  # noqa: F401
from .mongo import get_client, get_db, next_id  # noqa: F401
