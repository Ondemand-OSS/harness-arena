from __future__ import annotations

import asyncio
import os
import threading

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse
from starlette.types import Scope

from .cache import connection_status as cache_connection_status
from .cache import enabled as cache_enabled
from .db import get_db, init_db
from .logger import get_logger, log_error
from .routers import batches, config, deploy, harnesses, leaderboard, ondemand_models, runs, scores, stats, tasks, users
from .batches import reconcile_orphaned_batches
from .runner import reconciliation_loop, reconcile_orphaned_runs

log = get_logger("main")

app = FastAPI(title="Agentic Harness Arena", version="0.1.0")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Log unexpected errors and return a generic server response."""
    try:
        db = get_db()
        user_id = None
        token = request.headers.get("x-user-token")
        if token:
            from .users import _user_id_from_token  # local import avoids a cycle at module load

            user_id = _user_id_from_token(token, db)
        log_error(
            db,
            action="UNHANDLED_EXCEPTION",
            message=str(exc) or exc.__class__.__name__,
            user_id=user_id,
            error=exc,
            route=str(request.url.path),
            metadata={"method": request.method},
        )
    except Exception:
        log.exception("failed while logging an unhandled exception for %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})

# Long-lived background reconciliation task (see on_startup).
_reconciler: asyncio.Task | None = None

# Local development origins plus configured deployment origins.
_extra_origins = [o.strip() for o in os.environ.get("ARENA_CORS_ORIGINS", "").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", *_extra_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Arena-Cache", "X-Arena-Data-Source", "X-Arena-Cache-Message"],
)
# JSON list endpoints (tasks/board/leaderboard/overview) can run to hundreds
# of KB uncompressed; nothing else in this stack (no reverse proxy) gzips.
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.middleware("http")
async def allow_embedding(request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = "frame-ancestors *"
    return response


@app.on_event("startup")
def on_startup():
    db = get_db()
    init_db(db)
    # Reclaim work with expired execution leases.
    reconciled = reconcile_orphaned_runs(db)
    if reconciled:
        log.info("marked %d orphaned run(s) from a previous process as failed", reconciled)
    # Reconcile batches in the background to keep startup responsive.
    def _reconcile_batches_in_background() -> None:
        try:
            reconciled_batches = reconcile_orphaned_batches(db)
            if reconciled_batches:
                log.info("marked %d orphaned batch(es) from a previous process as failed", reconciled_batches)
        except Exception as exc:  # never let a background cleanup step take the process down
            log.error("background batch reconciliation failed: %s", exc, exc_info=True)

    threading.Thread(target=_reconcile_batches_in_background, daemon=True).start()

    # Keep a strong reference to the periodic reconciliation task.
    global _reconciler
    _reconciler = asyncio.create_task(reconciliation_loop())


app.include_router(users.router)
app.include_router(batches.router)
app.include_router(tasks.router)
app.include_router(config.router)
app.include_router(harnesses.router)
app.include_router(ondemand_models.router)
app.include_router(runs.router)
# Registered after runs.router: both are prefixed /api/runs, and these are
# distinct sub-paths (/{id}/preview, /{id}/project.zip) rather than
# overrides, so ordering only matters for keeping them grouped together.
app.include_router(deploy.router)
app.include_router(scores.router)
app.include_router(leaderboard.router)
app.include_router(stats.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "redis_cache_configured": cache_enabled()}


@app.get("/api/cache/status")
def cache_status():
    """Small public diagnostic response; it never exposes a Redis URL or token."""
    state = cache_connection_status()
    return {
        "redis_cache_configured": cache_enabled(),
        "redis_connection": state,
        "message": "Redis response cache is connected" if state == "connected" else "Redis response cache is not available",
        "cached_apis": [
            "GET /api/tasks",
            "GET /api/tasks/{task_id}",
            "GET /api/tasks/categories",
            "GET /api/tasks/groups",
            "GET /api/stats",
            "GET /api/harnesses",
            "GET /api/leaderboard",
            "GET /api/leaderboard/harness/{harness_key}",
            "GET /api/runs/board",
        ],
        "how_to_verify": "Call one cached API twice. The second response has X-Arena-Cache: HIT and X-Arena-Cache-Message: response fetched from Redis.",
    }


class SPAStaticFiles(StaticFiles):
    """Serve static assets and the SPA entry point for client-side routes."""

    async def get_response(self, path: str, scope: Scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or path == "api" or path.startswith("api/"):
                raise
            return FileResponse(os.path.join(self.directory, "index.html"))


# Optionally serve a built frontend directory after API routes.
_frontend_dist = os.environ.get(
    "ARENA_FRONTEND_DIST",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static"),
)
if os.path.isdir(_frontend_dist):
    app.mount("/", SPAStaticFiles(directory=_frontend_dist, html=True), name="frontend")
