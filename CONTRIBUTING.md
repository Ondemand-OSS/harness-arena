# Contributing

Thanks for taking a look at Agentic Harness Arena. This is an early-stage project — expect rough edges.

## Setup

See [README.md](README.md) for local setup. In short: `pip install -r backend/requirements.txt`, a MongoDB connection in `backend/.env`, then `uvicorn app.main:app --reload --port 8420`. The frontend is a separate repo — [harness-arena-fe](https://github.com/shrey2003/harness-arena-fe) — with its own setup and its own README.

## Ground rules

- **Never commit secrets.** Provider API keys are entered through the Setup page and stored in MongoDB, not in a tracked file. `backend/.env.example` and `backend/config.example.yaml` are references only — real credentials belong in `backend/.env` (gitignored) or your deployment's own secret store.
- **Never bake a real person's credential into a script default.** A hardcoded fallback password/email in a maintenance or seed script becomes a publicly-known default the moment this repo is public — see the git history around `scripts/import_seed_results.py`'s seed account for the exact failure mode this caused once already. Require the value explicitly (`argparse`'s `required=True`) or generate a random one at runtime instead.
- **Harness adapters stay interchangeable.** Every harness implements the same `HarnessAdapter` protocol (`backend/app/harnesses/base.py`). If you're adding an adapter, don't special-case it into the runner or API; register it in `harnesses/registry.py` and it should just work.
- **Elo is derived, not stored.** Don't add a mutable "current rating" field anywhere — `backend/app/elo.py` recomputes ratings from the `scores` collection's full history on every read, so the leaderboard can never drift from the raw judging history. If you change the scoring model, change it there.
- **Equal weight per task is a hard invariant.** The `scores` collection has a unique index on `(task_id, harness_key, user_id, provider_config_id)` (see `backend/app/mongo.py`) specifically so re-judging a task can't accidentally double-count it. Keep that constraint if you touch the scores schema.
- **A run/batch's execution is tied to whichever process started it — there is no separate job queue.** It runs as a plain `asyncio` background task, not something a second process can pick up or resume. If you touch `runner.py`/`batches.py`, read their ownership-lease comments (`hold_lease`, `reconcile_orphaned_runs`) before changing anything about how a run's status transitions — the wrong assumption here has already once caused live runs on one instance to get silently marked "failed" by another instance starting up alongside it.

## Reporting issues

Open a GitHub issue with what you were doing, what you expected, and what happened instead. For anything touching the blind-judging flow, include whether harness identity leaked anywhere it shouldn't have — that's the property this whole project depends on.
