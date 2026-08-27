# Agentic Harness Arena — Backend

An [LMSYS Chatbot Arena](https://chat.lmsys.org/)-style tool for comparing **agent harnesses** such as Claude Code and Codex CLI, rather than comparing models. The same task is handed to every selected harness under the same model and provider configuration, each in an isolated workspace. Review the deliverables blind, score them, and reveal the harness identities only after submitting your verdict. Every task has equal weight in the global Elo leaderboard.

> **Status: early / v0.** Claude Code and Codex CLI use real local CLI adapters. OnDemand runs live too, via its own hosted Chat/Agent API — see "OnDemand setup" below.

**This is the backend only.** The frontend is a separate repo — [harness-arena-fe](https://github.com/shrey2003/harness-arena-fe) — deployed independently (this backend on OnDemand Serverless or any Docker host, the frontend on Vercel). See "Deployment" below for how the two talk to each other.

## Why

Model leaderboards are everywhere. Harness leaderboards — which CLI actually gets the most useful work done on real, multi-deliverable tasks, holding the model constant — are not. This project blind-tests harnesses the way LMSYS blind-tests models: hide identity, collect human judgment, rank by Elo.

## Architecture

```
backend/    (this repo) FastAPI + MongoDB. Dataset import, run orchestration, blind-compare/scoring API, derived Elo leaderboard.
frontend/   github.com/shrey2003/harness-arena-fe — Vite + React + Tailwind. Arena, blind judging, leaderboard, harness roster, battle log, methodology pages.
```

- **Dataset**: tasks are imported from an `.xlsx` file with columns `id_aa, title, category, prompt, system_prompt, rubric, expected_deliverables, reference_files`. `expected_deliverables` accepts at most 20 comma-separated filenames per task. Re-importing upserts by `id_aa`.
- **Harness adapters** (`backend/app/harnesses/`): every adapter implements the same `run(task, workdir, provider) -> RunResult` interface. Built-in Claude Code and Codex adapters execute their respective CLIs. OnDemand calls its own hosted Chat/Agent API directly (session create + query) using the signed-in user's own OnDemand key and an admin-curated model whitelist — see "OnDemand setup" below. The backend also supports webhook-backed custom harnesses through `WebhookAdapter`.
- **Fresh benchmark runs**: every selected harness runs a task again, even if it completed the same task/model before. Earlier attempts remain in history, while judging uses the latest run per harness.
- **Blind judging**: `GET /api/scores/compare/{task_id}` labels the latest completed run per harness "Response A/B/C…" in an order seeded by task id (not harness identity). A signed-in user sees harness names only after submitting their own score; every user can judge a task once, and the public leaderboard aggregates every submitted verdict — see `backend/app/routers/scores.py`.
- **Leaderboard**: Elo ratings are never stored as source-of-truth — `backend/app/elo.py` recomputes them from the full score history. The compact API response may be held briefly in the optional Redis response cache and is invalidated whenever scores or completed runs change. Every task contributes equal weight to the leaderboard regardless of how many harnesses were compared on it (re-scoring a task upserts rather than double-counting).
- **AI judge score**: the judging UI has a slot for a second, automated score per deliverable next to your own — populated from a `JudgeVerdict` row when one exists (see "Seeding real results" below), shown only once identities are revealed so it can never anchor your own judgment. Empty (with a note like "not graded") until you seed judge results, or forever if you never do — your own score is always what drives the leaderboard.
- **Reasoning effort**: an admin-only knob — never shown to a regular user, even one running against the exact profile/model it's set on — that controls how hard a model thinks before answering. Free provider profiles and the OnDemand model whitelist each carry their own (`low`/`medium`/`high`/`xhigh`/`max`), forwarded as each harness's own native mechanism: Claude Code's `--effort` flag, Codex's `-c model_reasoning_effort=`, OnDemand's `reasoningEffort` query field. A free profile defaults to `medium` when unset; personal (non-free) profiles get no arena-imposed default at all. See `backend/app/routers/config.py` and `routers/ondemand_models.py`.

## Running locally

Requires Python 3.10+, MongoDB, and the local CLI binaries for every harness you intend to run. A free MongoDB Atlas cluster works well for development.

```bash
cd backend
cp .env.example .env   # then fill in MONGODB_URI (and MONGODB_DB_NAME if you want something other than "harness_arena")
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8420
```

Install the local harness CLIs before starting a live battle:

```bash
npm install -g @anthropic-ai/claude-code @openai/codex
```

For the frontend, clone [harness-arena-fe](https://github.com/shrey2003/harness-arena-fe) separately and follow its own README — its dev server proxies `/api/...` straight to `http://127.0.0.1:8420`, i.e. this backend running locally, with no env var needed for that case.

Once both are running, open `http://localhost:5173`, create an account, upload an `.xlsx` dataset from **New Benchmark**, and add a provider profile in **Setup**. Select at least two harnesses, run the benchmark, then judge completed results from **Evaluate**.

### User sessions

Successful signup/login returns a 10-minute access token that the frontend keeps only in memory. A separate opaque refresh token is stored in a 15-day `HttpOnly` cookie; MongoDB stores only its SHA-256 hash, user/session family, expiry, and revocation metadata. On a 401 the frontend rotates the refresh token, receives a new access token, and retries the original request once. Logout revokes the token family and clears the cookie; reuse of a rotated refresh token revokes the family as a theft precaution.

## Seeding real results

If you have pre-recorded deliverables and/or AI-judge results for this dataset, drop them in a gitignored `extras/` folder at the repo root. This is local-only input,
never committed, deliberately separate from anything the app ships with (useful if that data is
confidential):

```
extras/
  Multi_Source_Agent_Workflows-dataset.xlsx      (or wherever Setup's "import bundled dataset" looks)
  Harness_Bench_Deliverable_Outputs_Harness_Wise.zip   Harness_Bench_Outputs/<Harness Name>/<task_id>/<files>
  Harness_Bench_Judge_Results.zip                      Harness_Bench_Judge_Results/<Harness Name> Judgement/<task_id>.txt
```

Then, with the backend venv active:

```bash
cd backend
python -m scripts.import_seed_results
```

This creates `done` runs with their actual deliverable files for every
`(task, harness)` pair found, and parses the judge text files (a "×weight / earned/max / narrative"
rubric breakdown per criterion — see `app/judge_import.py`) into `JudgeVerdict` rows. It's safe to
re-run — each pair's seed run and verdict are replaced, not duplicated. None of this touches the
`scores` collection or Elo: those still come only from your own judging in the app. `app/benchmark_reference.py`
holds a small hardcoded table of aggregate reference metrics (mean score %, cost/task, tokens/task,
time/task) shown on a harness's profile page — edit it to match whatever summary numbers your own
benchmark run produced, or delete a harness's entry if you don't have one.

The first run also creates a submitter account (username `ondemand`) these seeded results are attributed to. Its password is a random one, printed once to the console when that account is first created — save it then, or set `ARENA_SEED_USER_PASSWORD` beforehand to choose your own. (This is deliberately not a fixed default: that username matches `ARENA_ADMIN_USERNAME`, and a fixed fallback string would be a publicly-known default admin password for anyone who forked this repo and ran the script without overriding it.)

`python -m scripts.reset_and_seed` does all of the above from scratch — drops the core collections, reimports the dataset, then runs the two seed scripts if their inputs are present in `extras/`. Refuses to run against a database that already holds real judging scores unless you pass `--force`.

## Adding a harness

**Have a hosted agent service you want in the arena?** The backend supports webhook-backed harnesses. It `POST`s each task, including the active model/provider configuration, and expects `{"ok": true, "deliverables": [{"filename": "...", "content_base64": "..."}]}` in return. See `backend/app/harnesses/webhook.py` for the contract. Only point this at a webhook you trust because the provider API key is included in each run request.

**Adding a local CLI adapter to the codebase:**

1. Implement `HarnessAdapter` (see `backend/app/harnesses/base.py`) — `run()` should spawn the real CLI subprocess in the given `workdir` and return the deliverable filenames it produced.
2. Register it in `backend/app/harnesses/registry.py`'s `BUILTIN` dict.
3. Nothing else changes — the runner, API, and frontend are adapter-agnostic.

`backend/app/harnesses/claude_code.py` and `codex_cli.py` are themselves the reference for the invocation shape a new local CLI adapter would follow (env vars for provider base URL/API key, `--output-format stream-json` / `--json`, sandbox flags).

## Deployment

The backend and frontend are separate repos, deployed separately:

1. **This backend on OnDemand Serverless** (or any other Docker host):
   - Point the platform's "Dockerfile Path" at `backend/Dockerfile`. The `COPY` paths inside it are relative to the repo root, since most single-field "Dockerfile path" platforms (OnDemand Serverless included) build with the repo root as context regardless of where the Dockerfile itself lives — nothing else to configure for build context.
   - The container reads the platform's own `PORT` env var and binds to it directly (falls back to `8420` if `PORT` isn't set, e.g. a local `docker run`) — no manual "Target Port" value needed.
   - Set `ARENA_CORS_ORIGINS` to the frontend's real domain(s), comma-separated (e.g. `https://your-app.vercel.app`), so the browser is allowed to call this API cross-origin.
   - For the 15-day `HttpOnly` refresh cookie in a split-domain deployment, set `ARENA_COOKIE_SECURE=true` and `ARENA_REFRESH_COOKIE_SAMESITE=none`. The frontend origin must appear exactly in `ARENA_CORS_ORIGINS`; wildcard origins are not compatible with credentialed cookie requests.
   - Optional but recommended for a remote/free MongoDB cluster: set `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`, and `ARENA_CACHE_PREFIX=harness-arena:prod:v1`. This caches only compact public JSON such as tasks, filters, stats, harnesses, and leaderboard responses. Use a different prefix locally; the application works normally without Redis.
   - "Instances Count" — see **Execution model & scaling** below before setting this above `1`. Storage-wise every instance is fine sharing the same Mongo database; the caveats are about in-flight run/batch execution, not persistence.
2. **The frontend on Vercel**: clone [harness-arena-fe](https://github.com/shrey2003/harness-arena-fe), import it as its own Vercel project, and set its `VITE_API_BASE` env var to this backend's public URL. See that repo's own README for the full deploy steps (it also ships a Dockerfile, for a non-Vercel host).

### Persistence

The backend stores everything in **MongoDB** (`MONGODB_URI`/`MONGODB_DB_NAME` in `backend/.env`) — tasks, runs, scores, users, batches, and **deliverable file bytes** (stored directly in the `deliverables` collection as binary, not on disk). Local disk is only ever used as an ephemeral staging directory during a single run (created and deleted within one request) — nothing persists there between requests, so redeploying or restarting is always safe: there's no local state to lose. Scaling to multiple instances is a separate question from persistence — see **Execution model & scaling** below.

Upstash Redis is an optional speed layer, never primary storage. It contains no deliverable bytes, passwords, API keys, sessions, private provider configuration, or user-specific responses. Cache failures fall through to MongoDB, short TTLs bound staleness, and write routes invalidate the affected response namespaces.

If you point this at a shared Atlas cluster that also hosts unrelated databases, the app only ever touches the single database named by `MONGODB_DB_NAME` — it never enumerates or writes to sibling databases. Worth double-checking yourself before pointing a fresh install at a cluster you don't fully control.

### Execution model & scaling

There is no separate job queue or worker tier. A battle (or a whole-dataset batch) runs as a plain `asyncio` background task inside whichever process handled the request that triggered it — the request itself returns immediately with the run rows still `pending`; the frontend polls their status rather than waiting on that response. This keeps the architecture simple, but it means a run/batch's execution is tied to the lifetime of the one process that started it.

Running more than one instance used to be actively dangerous for exactly that reason: if a second instance started up while the first was mid-battle, its own startup cleanup couldn't tell "an orphaned run from a dead process" apart from "a live run a sibling instance is actively working on," and would mark the live one failed out from under it. Two mechanisms now guard against that:

- **Ownership leases.** Every run/batch is stamped with the id of the process executing it and a heartbeat refreshed every `ARENA_HEARTBEAT_INTERVAL_SECONDS` (default 15s). Cleanup — both at startup and on a recurring `ARENA_RECONCILE_INTERVAL_SECONDS` sweep (default 60s) — only reclaims a row once its lease has gone unrefreshed for `ARENA_HEARTBEAT_STALE_AFTER_SECONDS` (default 90s), never a live sibling's in-flight work.
- **A fleet-wide concurrency cap.** `ARENA_MAX_CONCURRENT_RUNS` (default 5) still bounds concurrency *within* one process; `ARENA_GLOBAL_MAX_CONCURRENT_RUNS` (defaults to the same value) additionally caps how many runs may be `status: "running"` **across every instance combined**, polled every `ARENA_SLOT_POLL_INTERVAL_SECONDS` (default 2s) — so N instances no longer means N× the real CLI subprocesses on the underlying host with no coordination between them. This is a soft cap (a brief window where two runs can both see room and both proceed slightly over), not an atomic one, by design — see `backend/app/runner.py`'s `_acquire_global_slot`.

What this does **not** yet cover, if you're considering unpinning "Instances Count":

- An orphaned run (its owning instance genuinely died) is reclaimed as failed for manual retry — nothing auto-resumes it on another instance.
- Lease comparisons use client-generated timestamps, not Mongo's server clock — fine for one instance, worth hardening (`$currentDate`) before real multi-instance clock skew is a factor.

Set "Instances Count" to `1` unless you've read the above and are comfortable with those two gaps.

A few other execution knobs, all optional: `ARENA_HARNESS_TIMEOUT_SECONDS` (default 3600) is the per-harness wall-clock kill timeout — a run that produces no new/updated deliverable for this long is killed and marked failed. `ARENA_CLAUDE_BIN`/`ARENA_CODEX_BIN` override the CLI binary name/path if it isn't just `claude`/`codex` on `PATH`.

## OnDemand setup

OnDemand doesn't fit the shared model+base_url+api_key provider profile every other harness uses — its credential is personal to each signed-in user, and its "model" is one of a small admin-curated set of `endpointId` values (the API only accepts specific predefined strings, not free text):

1. Each user sets their **own** OnDemand API key in **Setup** (never shared with other accounts).
2. The arena admin curates the selectable model list, also in **Setup** — a label plus the OnDemand `endpointId` (e.g. `predefined-deepseek-v4-flash`), and optionally that model's reasoning effort (see "Reasoning effort" above) — neither is shown to a regular user beyond the label itself.
3. When OnDemand is part of a battle, the model picker requires choosing one of those admin-curated models, and warns if it doesn't look like the same underlying model as the shared provider profile the other harnesses are using.

If OnDemand's answer references generated file URLs (its own blob-storage links), the adapter downloads and matches them to the task's expected deliverable filenames automatically; otherwise the raw answer text becomes the one deliverable.

## Roadmap

- Wire up the "AI judge score" from Artificial Analysis (currently a "Coming soon" placeholder next to your own score in the judging UI).
- Richer deliverable diffing/preview in the judging UI.

## License

MIT — see [LICENSE](LICENSE).
