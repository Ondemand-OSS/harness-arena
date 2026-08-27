"""One-off local script: attaches real reference-file bytes to every
existing task whose `reference_files` text names a file present locally.

Reads plain files (not tracked in git — same `extras/` convention as
`import_seed_results.py`'s zips) from:

    extras/reference_files/<filename>

For every task in the `tasks` collection, this splits `reference_files`
the same way `expected_deliverables` is split (comma-separated names — see
taxonomy.parse_reference_filenames) and, for each name that has a matching
file in that directory, reads its bytes and upserts a
`task_reference_files` document for that (task_id, filename) pair — same
storage a manual upload via `POST /api/tasks/{id}/reference-files` would
create (see routers/tasks.py), so a run picks it up identically either way
(harnesses/_reference_files.py, harnesses/ondemand.py's session upload).

A task naming a file that isn't present locally is skipped with a warning,
not an error — this script is meant to backfill whatever reference
material happens to be available, not to enforce that every task has it.

Usage (from backend/, with the venv active):
    python -m scripts.import_reference_files
    python -m scripts.import_reference_files --dir path/to/reference_files
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_db, next_id  # noqa: E402
from app.runner import _guess_media_type  # noqa: E402
from app.taxonomy import parse_reference_filenames  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_REFERENCE_FILES_DIR = os.path.join(REPO_ROOT, "extras", "reference_files")


def run(reference_files_dir: str) -> None:
    db = get_db()
    available = {
        name: os.path.join(reference_files_dir, name)
        for name in os.listdir(reference_files_dir)
        if os.path.isfile(os.path.join(reference_files_dir, name))
    } if os.path.isdir(reference_files_dir) else {}

    if not available:
        print(f"no files found in {reference_files_dir} — nothing to import")
        return

    attached = 0
    missing: set[str] = set()
    for task in db.tasks.find({"is_deleted": {"$ne": True}}, {"_id": 1, "reference_files": 1}):
        for filename in parse_reference_filenames(task.get("reference_files", "")):
            path = available.get(filename)
            if path is None:
                missing.add(filename)
                continue
            with open(path, "rb") as f:
                content = f.read()
            doc = {
                "task_id": task["_id"],
                "filename": filename,
                "media_type": _guess_media_type(filename),
                "size_bytes": len(content),
                "content": content,
                "uploaded_at": dt.datetime.now(dt.timezone.utc),
            }
            existing = db.task_reference_files.find_one({"task_id": task["_id"], "filename": filename}, {"_id": 1})
            if existing:
                db.task_reference_files.update_one({"_id": existing["_id"]}, {"$set": doc})
            else:
                doc["_id"] = next_id(db, "task_reference_files")
                db.task_reference_files.insert_one(doc)
            attached += 1

    print(f"attached {attached} reference file(s) across matching tasks from {reference_files_dir}")
    if missing:
        print(
            "named by a task's reference_files but not found in that directory (skipped): "
            + ", ".join(sorted(missing))
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default=DEFAULT_REFERENCE_FILES_DIR, help="directory of reference files to import")
    args = parser.parse_args()
    run(args.dir)


if __name__ == "__main__":
    main()
