# Agentic Harness Arena

**Benchmark AI coding harnesses on the work they actually produce.**

Agentic Harness Arena runs the same task through multiple agent harnesses, collects their deliverables in isolated workspaces, and lets people evaluate the results blind. Once scores are submitted, harness identities are revealed and results contribute to an Elo-based leaderboard.

## Why Harness Arena?

The model is only one part of an agentic workflow. The harness—the CLI, tools, prompts, permissions, and execution environment—also shapes the quality of the final result. Harness Arena makes that comparison repeatable and fair.

## Features

- Run the same benchmark task across multiple harnesses.
- Execute work in isolated workspaces.
- Import task datasets from Excel (`.xlsx`).
- Review results blind before identities are revealed.
- Build an Elo leaderboard from human judgments.
- Extend the platform with local CLI or webhook-backed harnesses.
- Store application data and deliverables in MongoDB, with optional Redis caching.

## Quick start

### Prerequisites

- Python 3.10+
- A MongoDB instance
- Node.js, if you plan to run CLI harnesses

### Run locally

```bash
cd backend
cp .env.example .env
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8420
```

Set `MONGODB_URI` in `backend/.env` before starting the service. Keep `.env` files and real credentials out of Git.

Install the CLI tools you want to benchmark, for example:

```bash
npm install -g @anthropic-ai/claude-code @openai/codex
```

## Configuration

Use [`backend/.env.example`](backend/.env.example) as the reference for environment variables. For production, configure a strong `ARENA_SESSION_SECRET`, secure cookie settings, and `ARENA_CORS_ORIGINS` for trusted client domains.

Provider profiles are configured through the application. Prefer your deployment platform's secret store for production credentials.

## Deployment

Deploy the service with [`backend/Dockerfile`](backend/Dockerfile) on any Docker-compatible platform. The application reads `PORT` from the environment and falls back to `8420` locally.

MongoDB is the primary datastore for tasks, runs, scores, users, and deliverables. Redis is optional and caches compact API responses only.

## Add a harness

Local CLI harnesses implement the `HarnessAdapter` interface in [`backend/app/harnesses`](backend/app/harnesses), then register in `registry.py`. For hosted services, implement the webhook contract in [`webhook.py`](backend/app/harnesses/webhook.py).

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening an issue or pull request.

## License

Released under the [MIT License](LICENSE).
