# Agentic Harness Arena

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](backend/requirements.txt)
[![Status: early / v0](https://img.shields.io/badge/status-early%20%2F%20v0-orange.svg)](#status)
[![Discord](https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&logoColor=white)](https://discord.gg/fhGPEaDJ5T)

**An [LMSYS Chatbot Arena](https://chat.lmsys.org/)-style blind benchmark — but for agent harnesses, not models.**

**[🔴 Try the live arena →](https://www.harness-arena.ai)**


<img width="1282" height="852" alt="Agentic Harness Arena — blind judging UI" src="https://github.com/user-attachments/assets/12298b16-80d1-4fc2-a5cb-25c5ce7a53ce" />

Agentic Harness Arena runs the same task through multiple agent harnesses — Claude Code, Codex CLI, and more — holding the model constant, collects their deliverables in isolated workspaces, and lets people judge the results blind. Identities are only revealed after a score is submitted, and every verdict rolls up into a public Elo leaderboard.

If you've used Chatbot Arena to compare *models*, this is the same idea one layer up: comparing the *harness* — the CLI, tools, prompts, permissions, and execution environment wrapped around a model.

## Contents

- [Why Harness Arena?](#why-harness-arena)
- [How a run works](#how-a-run-works)
- [Supported harnesses](#supported-harnesses)
- [Architecture](#architecture)
- [Features](#features)
- [Quick start](#quick-start)
- [Dataset format](#dataset-format)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Add a harness](#add-a-harness)
- [Status](#status)
- [Contributing](#contributing)
- [License](#license)

## Why Harness Arena?

The model is only one part of an agentic workflow. The harness — the CLI, tools, prompts, permissions, and execution environment — shapes the quality of the final result just as much. Two harnesses running the identical model on the identical task can hand back very different work. Harness Arena makes that comparison repeatable, fair, and blind:

- **Same task, same model, isolated workspaces** — every selected harness runs the identical prompt under the identical provider config, each in its own sandboxed workdir. No harness sees another's output.
- **Blind before judged** — outputs are shown as anonymous "Response A/B/C…" until a score is submitted. Identities reveal only afterward, so nobody's judgment is anchored by which tool they *expect* to win.
- **One vote per person per task** — every signed-in user judges a task once; the public leaderboard aggregates every submitted verdict, not just an admin's.
- **Elo, recomputed, never stored as truth** — ratings are derived fresh from the full score history on every read, so a correction or a re-score can never leave a stale number on the board.

## How a run works

1. A task (prompt, rubric, expected deliverables) is picked from an imported dataset.
2. Every selected harness runs it independently, same model and provider config, each in its own workdir — no harness can see another's output or in-progress work.
3. Each harness's produced files are collected as its deliverables once it finishes.
4. A judge opens the task and sees every harness's output labeled anonymously ("Response A/B/C…"), in an order seeded by task id rather than harness identity.
5. The judge scores every deliverable. Only on submission are harness identities revealed — never before, so nothing about which tool is which can bias the score.
6. The verdict is stored and folded into the Elo leaderboard, which is recomputed from the full score history rather than incrementally patched.

## Supported harnesses

Built-in today, no extra setup beyond an API key:

| Harness | Type |
|---|---|
| [Claude Code](https://github.com/anthropics/claude-code) | Local CLI |
| [Codex CLI](https://github.com/openai/codex) | Local CLI |
| [OpenClaw](https://openclaw.ai) | Local CLI |
| [Hermes](https://hermes-agent.nousresearch.com) | Local CLI |
| [opencode](https://opencode.ai) | Local CLI |
| [OnDemand](https://on-demand.io) | Hosted API |

Anything else — your own agent, an internal tool, a hosted service — plugs in as a webhook-backed harness from the Setup UI, no code change or redeploy required. See [Add a harness](#add-a-harness).

## Architecture

```
backend/    (this repo) FastAPI + MongoDB. Dataset import, run orchestration,
            blind-compare/scoring API, derived Elo leaderboard.
frontend/   Vite + React + Tailwind, deployed separately. Arena, blind
            judging, leaderboard, harness roster, battle log.
```

**This repository is the backend only** — the API and orchestration behind the UI shown above. The frontend that renders it is a separate service, deployed independently of this repo.

## Features

- Run the same benchmark task across multiple harnesses under one shared model/provider config.
- Execute every harness's work in an isolated workspace.
- Import task datasets from Excel (`.xlsx`).
- Review results blind before identities are revealed.
- Build an Elo leaderboard from human judgments, recomputed from full history.
- Extend the platform with local CLI or webhook-backed harnesses — no core code change needed for the latter.
- Store application data and deliverables in MongoDB, with optional Redis caching.

## Quick start

### Prerequisites

- Python 3.10+
- A MongoDB instance (a free Atlas cluster works well for development)
- Node.js, if you plan to run local CLI harnesses (Claude Code, Codex, OpenClaw, Hermes, opencode)

### Run locally

```bash
cd backend
cp .env.example .env
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8420
```

Set `MONGODB_URI` in `backend/.env` before starting the service. Keep `.env` files and real credentials out of Git.

Once it's running, open `http://127.0.0.1:8420/docs` for interactive Swagger UI over the full API — dataset import, run orchestration, scoring, the leaderboard — no extra setup, it ships with FastAPI.

Install the local CLI tools you want to benchmark, for example:

```bash
npm install -g @anthropic-ai/claude-code @openai/codex
```

This service is the API and orchestration layer — the endpoints above are enough to run tasks against directly. Judging blind and browsing the leaderboard visually needs a frontend pointed at this backend's `/api`.

## Dataset format

Tasks are imported from an `.xlsx` workbook with one `Tasks` sheet:

| Column | Meaning |
|---|---|
| `id_aa` | Stable task id — re-importing the same id upserts that task |
| `title` | Shown to judges |
| `category` | Grouping shown on the leaderboard and task browser |
| `prompt` | What every harness is actually given |
| `system_prompt` | Optional system-level framing |
| `rubric` | Judging guidance shown alongside the task |
| `expected_deliverables` | Comma-separated filenames, up to 20 per task |
| `reference_files` | Comma-separated filenames of supplementary source material handed to every harness alongside the prompt |

## Configuration

Use [`backend/.env.example`](backend/.env.example) as the reference for environment variables. For production, configure a strong `ARENA_SESSION_SECRET`, secure cookie settings, and `ARENA_CORS_ORIGINS` for trusted client domains.

Provider profiles (models, API keys, reasoning effort) are configured through the application itself, not environment variables. Prefer your deployment platform's secret store for production credentials.

## Deployment

Deploy the service with [`backend/Dockerfile`](backend/Dockerfile) on any Docker-compatible platform. The application reads `PORT` from the environment and falls back to `8420` locally.

MongoDB is the primary datastore for tasks, runs, scores, users, and deliverables. Redis is optional and caches compact API responses only — nothing depends on it being present.

## Add a harness

- **Local CLI**: implement the `HarnessAdapter` interface in [`backend/app/harnesses`](backend/app/harnesses), then register it in `registry.py`.
- **Hosted service**: no code required — implement the webhook contract described in [`webhook.py`](backend/app/harnesses/webhook.py) and register it from the Setup UI.

## Status

Early / v0. Claude Code, Codex CLI, and OnDemand run real, live comparisons today; the rest of the built-in roster is under active development. Expect rough edges — [issues](../../issues) and PRs are genuinely welcome, not just tolerated.

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening an issue or pull request.

For questions, ideas, or discussion, join us on [Discord](https://discord.gg/fhGPEaDJ5T).

If you find this useful, a ⭐ on the repo helps others find it.

## License

Released under the [MIT License](LICENSE).
