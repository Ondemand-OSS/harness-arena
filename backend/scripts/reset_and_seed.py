"""Drop and rebuild the arena's collections in MongoDB, then reseed.

There's no migration framework here, and Mongo has no schema to ALTER
anyway — but while document shapes are still moving it's simplest to wipe
and rebuild: everything is either re-importable (the dataset) or
re-derivable (the seeded runs and judge verdicts).

    python -m scripts.reset_and_seed

Only the collections listed in `COLLECTIONS_TO_DROP` are touched, and only
inside the one database this app is scoped to (`MONGODB_DB_NAME` — see
mongo.py). This is deliberately NOT a `client.drop_database()` call: this
Atlas cluster may host other, unrelated databases, and a blanket drop is
exactly the kind of command that must never be one typo away from wiping
someone else's data.

The one thing this DOES destroy is human judging history (scores) and any
user accounts, so it refuses to run without --force once scores exist.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.dataset_import import import_xlsx  # noqa: E402
from app.db import get_client, get_db, init_db  # noqa: E402
from app.mongo import MONGODB_DB_NAME  # noqa: E402
from scripts import import_reference_files, import_seed_results  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATASET_CANDIDATES = [
    os.path.join(REPO_ROOT, "extras", "Multi_Source_Agent_Workflows-dataset.xlsx"),
    os.path.join(REPO_ROOT, "Multi_Source_Agent_Workflows-dataset.xlsx"),
]

COLLECTIONS_TO_DROP = [
    "tasks",
    "runs",
    "deliverables",
    "task_reference_files",
    "scores",
    "judge_verdicts",
    "users",
    "batches",
    "custom_harnesses",
    "provider_config",
    "ondemand_models",
    # Per-user daily submission quota ledger (rate_limit.py). Dropped with
    # the users it refers to, so a reset doesn't leave orphaned quota rows
    # charging a fresh account for a previous one's submissions.
    "task_submissions",
    "counters",
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="drop the database even if it holds judging history")
    args = parser.parse_args()

    db = get_db()
    print(f"Target: {get_client().address} / database {MONGODB_DB_NAME!r}")

    score_count = db.scores.count_documents({})
    if score_count and not args.force:
        raise SystemExit(
            f"refusing to reset: the database holds {score_count} judging score(s).\n"
            "Re-run with --force if you're sure you want to discard them."
        )

    for name in COLLECTIONS_TO_DROP:
        db.drop_collection(name)
    print(f"dropped collections: {', '.join(COLLECTIONS_TO_DROP)}")

    init_db(db)
    print("recreated indexes")

    dataset = next((p for p in DATASET_CANDIDATES if os.path.isfile(p)), None)
    if dataset is None:
        print("no dataset found — skipping import (load one from the app's Benchmark page)")
    else:
        count, _new_task_ids, _skipped_existing_ids = import_xlsx(dataset, db)
        print(f"imported {count} tasks from {dataset}")

    if os.path.isfile(import_seed_results.DEFAULT_DELIVERABLES_ZIP) and os.path.isfile(
        import_seed_results.DEFAULT_JUDGE_ZIP
    ):
        import_seed_results.run(
            import_seed_results.DEFAULT_DELIVERABLES_ZIP, import_seed_results.DEFAULT_JUDGE_ZIP
        )
    else:
        print("no seed zips in extras/ — skipping real deliverables/judge import")

    if os.path.isdir(import_reference_files.DEFAULT_REFERENCE_FILES_DIR):
        import_reference_files.run(import_reference_files.DEFAULT_REFERENCE_FILES_DIR)
    else:
        print("no extras/reference_files/ — skipping reference-file attachment")

    print("done.")


if __name__ == "__main__":
    main()
