"""Backfill score ownership fields and remove obsolete score indexes.

Usage: ``python -m scripts.migrate_legacy_scores``.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_db  # noqa: E402


def main() -> None:
    db = get_db()
    # A person can judge a task once; different people may each submit
    # their own blind verdict. Preserve pre-user-scoping score documents as
    # legacy public results rather than discarding them during migration.
    result_user = db.scores.update_many({"user_id": {"$exists": False}}, {"$set": {"user_id": 0}})
    result_provider = db.scores.update_many({"provider_config_id": {"$exists": False}}, {"$set": {"provider_config_id": None}})
    print(f"scores: backfilled user_id on {result_user.modified_count}, provider_config_id on {result_provider.modified_count}")

    existing = {index["name"] for index in db.scores.list_indexes()}
    for old_index in ("task_id_1_harness_key_1", "task_id_1_harness_key_1_user_id_1"):
        if old_index in existing:
            db.scores.drop_index(old_index)
            print(f"scores: dropped legacy index {old_index}")


if __name__ == "__main__":
    main()
