"""One-off local script: seeds the arena with real pre-recorded deliverables
and judge results.

Reads two zip files (not tracked in git — see the repo root's `extras/`,
which is gitignored specifically because this kind of input can be
confidential):

- Harness_Bench_Deliverable_Outputs_Harness_Wise.zip
    Harness_Bench_Outputs/<Harness Name>/<task_id>/<deliverable files...>
- Harness_Bench_Judge_Results.zip
    Harness_Bench_Judge_Results/<Harness Name> Judgement/<task_id>.txt

For each (task_id, harness) pair found, this creates a `done`,
`source="seed"` run document + its deliverable documents (file bytes read
straight out of the zip into MongoDB — no local staging directory needed at
all, replacing any previous seed run for that pair so re-running this
script refreshes seed data rather than accumulating it), and upserts a
judge-verdict document parsed from the matching judge text file.

This never touches the `scores` collection or Elo — those still only come
from a human's own judging in the app, per the design goal that the AI
judge is reference material, not something that can move the leaderboard
itself.

Usage (from backend/, with the venv active):
    python -m scripts.import_seed_results
    python -m scripts.import_seed_results --deliverables path/to.zip --judge path/to.zip
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_db, init_db, next_id  # noqa: E402
from app.judge_import import parse_rubric_text  # noqa: E402
from app.runner import _guess_media_type  # noqa: E402
from app.users import hash_password  # noqa: E402
import datetime as dt  # noqa: E402

# The pre-recorded benchmark is attributed to this account, so the arena can
# show who submitted each task. It shares its name with the default
# ARENA_ADMIN_USERNAME, so its password is effectively the arena admin's
# password — this project being open source means a fixed fallback string
# here would be a publicly known default admin credential for anyone who
# runs this script without setting ARENA_SEED_USER_PASSWORD first. A random
# one is generated instead (see main()) and printed once, only when the
# account is actually being created.
SEED_USERNAME = "ondemand"
SEED_DISPLAY_NAME = "OnDemand"
SEED_AVATAR_KEY = "ondemand"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DELIVERABLES_ZIP = os.path.join(REPO_ROOT, "extras", "Harness_Bench_Deliverable_Outputs_Harness_Wise.zip")
DEFAULT_JUDGE_ZIP = os.path.join(REPO_ROOT, "extras", "Harness_Bench_Judge_Results.zip")

HARNESS_FOLDER_TO_KEY = {
    "Claude Code Harness": "claude-code",
    "Codex Harness": "codex",
    "Ondemand Harness": "ondemand",
}
JUDGE_FOLDER_TO_KEY = {
    "Claude Code Harness Judgement": "claude-code",
    "Codex Harness Judgement": "codex",
    "Ondemand Harness Judgement": "ondemand",
}


def _load_deliverables(zip_path: str) -> dict[tuple[str, str], list[tuple[str, bytes]]]:
    """Returns {(task_id, harness_key): [(filename, content_bytes), ...]}
    read straight out of the zip — no filesystem writes at all, since the
    destination (MongoDB) doesn't need a local file to exist first."""
    out: dict[tuple[str, str], list[tuple[str, bytes]]] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            parts = info.filename.split("/")
            # Harness_Bench_Outputs/<Harness Name>/<task_id>/<filename>
            if len(parts) < 4:
                continue
            harness_folder, task_id, filename = parts[1], parts[2], parts[-1]
            harness_key = HARNESS_FOLDER_TO_KEY.get(harness_folder)
            if harness_key is None:
                print(f"  skip: unrecognized harness folder {harness_folder!r}")
                continue
            content = zf.read(info)
            out.setdefault((task_id, harness_key), []).append((filename, content))
    return out


def _load_judge_results(zip_path: str) -> dict[tuple[str, str], str]:
    """Returns {(task_id, harness_key): raw judge text}."""
    out: dict[tuple[str, str], str] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            parts = info.filename.split("/")
            # Harness_Bench_Judge_Results/<Harness Name> Judgement/<task_id>.txt
            if len(parts) < 3:
                continue
            judge_folder, filename = parts[1], parts[-1]
            harness_key = JUDGE_FOLDER_TO_KEY.get(judge_folder)
            if harness_key is None or not filename.endswith(".txt"):
                continue
            task_id = filename[: -len(".txt")]
            out[(task_id, harness_key)] = zf.read(info).decode("utf-8", errors="replace")
    return out


def run(deliverables_zip: str, judge_zip: str) -> None:
    if not os.path.isfile(deliverables_zip):
        raise SystemExit(f"deliverables zip not found: {deliverables_zip}")
    if not os.path.isfile(judge_zip):
        raise SystemExit(f"judge results zip not found: {judge_zip}")

    db = get_db()
    init_db(db)

    print(f"Reading deliverables from {deliverables_zip} ...")
    deliverables_by_pair = _load_deliverables(deliverables_zip)
    print(f"  found {len(deliverables_by_pair)} (task, harness) pairs")

    print(f"Reading judge results from {judge_zip} ...")
    judge_text_by_pair = _load_judge_results(judge_zip)
    print(f"  found {len(judge_text_by_pair)} (task, harness) pairs")

    # The account the pre-recorded benchmark is credited to. Created once;
    # an existing account's password is never overwritten by re-running.
    seed_user = db.users.find_one({"username": SEED_USERNAME})
    if seed_user is None:
        password = os.environ.get("ARENA_SEED_USER_PASSWORD") or secrets.token_urlsafe(16)
        seed_user_id = next_id(db, "users")
        seed_user = {
            "_id": seed_user_id,
            "username": SEED_USERNAME,
            "username_lower": SEED_USERNAME.lower(),
            # No email here on purpose — this is a project-agnostic seed
            # script, so there's no real address to give it, and the
            # users.email_lower index is sparse specifically to allow that.
            # Set one directly in Mongo (or sign up fresh with this
            # username through the normal verification flow) if you want
            # this account to have one.
            "display_name": SEED_DISPLAY_NAME,
            "password_hash": hash_password(password),
            "avatar_key": SEED_AVATAR_KEY,
            "created_at": dt.datetime.now(dt.timezone.utc),
        }
        db.users.insert_one(seed_user)
        print(f"Created submitter account '{SEED_USERNAME}' (password: {password!r}) — save this now, it is shown only once.")
    else:
        print(f"Submitter account '{SEED_USERNAME}' already exists; password left unchanged.")

    known_task_ids = {t["_id"] for t in db.tasks.find({}, {"_id": 1})}

    runs_created = 0
    for (task_id, harness_key), files in sorted(deliverables_by_pair.items()):
        if task_id not in known_task_ids:
            print(f"  skip {task_id}/{harness_key}: no matching task in the dataset")
            continue

        # Credit the task to the seed account (only if unattributed, so a
        # task someone else later submits isn't reassigned).
        db.tasks.update_one(
            {"_id": task_id, "submitted_by_user_id": None},
            {"$set": {"submitted_by_user_id": seed_user["_id"]}},
        )

        # Replace any previous seed run for this pair — re-running this
        # script is meant to refresh seed data, not accumulate it.
        old_run_ids = [r["_id"] for r in db.runs.find({"task_id": task_id, "harness_key": harness_key, "source": "seed"}, {"_id": 1})]
        if old_run_ids:
            db.deliverables.delete_many({"run_id": {"$in": old_run_ids}})
            db.runs.delete_many({"_id": {"$in": old_run_ids}})

        run_id = next_id(db, "runs")
        db.runs.insert_one(
            {
                "_id": run_id,
                "task_id": task_id,
                "harness_key": harness_key,
                "status": "done",
                "started_at": None,
                "finished_at": dt.datetime.now(dt.timezone.utc),
                "error_message": "",
                "source": "seed",
                "created_at": dt.datetime.now(dt.timezone.utc),
            }
        )

        for filename, content in files:
            db.deliverables.insert_one(
                {
                    "_id": next_id(db, "deliverables"),
                    "run_id": run_id,
                    "filename": filename,
                    "relpath": filename,
                    "media_type": _guess_media_type(filename),
                    "size_bytes": len(content),
                    "content": content,
                }
            )
        runs_created += 1

    verdicts_created = 0
    for (task_id, harness_key), raw_text in sorted(judge_text_by_pair.items()):
        if task_id not in known_task_ids:
            continue
        parsed = parse_rubric_text(raw_text)
        breakdown = [
            {"name": c.name, "weight": c.weight, "earned": c.earned, "max": c.max, "narrative": c.narrative}
            for c in parsed.criteria
        ]
        db.judge_verdicts.update_one(
            {"task_id": task_id, "harness_key": harness_key},
            {
                "$set": {
                    "score": parsed.score,
                    "note": parsed.note,
                    "breakdown": breakdown,
                    "source": "Artificial Analysis judge (pre-recorded)",
                    "imported_at": dt.datetime.now(dt.timezone.utc),
                },
                "$setOnInsert": {"_id": next_id(db, "judge_verdicts")},
            },
            upsert=True,
        )
        verdicts_created += 1

    print(f"Seeded {runs_created} real runs and {verdicts_created} judge verdicts.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deliverables", default=DEFAULT_DELIVERABLES_ZIP)
    parser.add_argument("--judge", default=DEFAULT_JUDGE_ZIP)
    args = parser.parse_args()
    run(args.deliverables, args.judge)
