"""Run orchestration, workspaces, concurrency limits, and execution leases."""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import os
import shutil
import tempfile
import time
import types
import uuid
from typing import Callable

from pymongo.database import Database

from .cache import invalidate as cache_invalidate
from .db import get_client, next_id
from .harnesses.base import ProviderSettings, RunResult
from .harnesses.registry import get_adapter
from .logger import get_logger, log_error
from .mongo import MONGODB_DB_NAME
from .ondemand_skills import download_and_extract_skills

log = get_logger("runner")
from .routers.config import effective_reasoning_effort
from .routers.ondemand_models import suggest_plugins_enabled
from .taxonomy import parse_deliverables


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# Assumed lifetime of a provider-created preview URL.
HARNESS_PREVIEW_TTL_SECONDS = int(os.environ.get("ARENA_HARNESS_PREVIEW_TTL_SECONDS", str(90 * 60)))


# Per-process cap for active adapter subprocesses.
MAX_CONCURRENT_RUNS = max(1, int(os.environ.get("ARENA_MAX_CONCURRENT_RUNS", "20")))
_run_slots = asyncio.Semaphore(MAX_CONCURRENT_RUNS)

# Fleet-wide soft cap based on currently running records.
GLOBAL_MAX_CONCURRENT_RUNS = max(
    1, int(os.environ.get("ARENA_GLOBAL_MAX_CONCURRENT_RUNS", str(MAX_CONCURRENT_RUNS)))
)
SLOT_POLL_INTERVAL_SECONDS = max(0.5, float(os.environ.get("ARENA_SLOT_POLL_INTERVAL_SECONDS", "2")))

# Deliverable bytes are stored inline on a `deliverables` document, and Mongo
# hard-rejects any document over 16MB (DocumentTooLarge). Capped well under
# that so other document fields and BSON overhead never push a borderline
# file over the real limit.
MAX_DELIVERABLE_BYTES = int(os.environ.get("ARENA_MAX_DELIVERABLE_BYTES", str(15 * 1024 * 1024)))


async def _acquire_global_slot(db: Database, stop_requested) -> bool:
    """Wait for a fleet-wide slot, unless execution is stopped."""
    while True:
        if stop_requested():
            return False
        if db.runs.count_documents({"status": "running"}) < GLOBAL_MAX_CONCURRENT_RUNS:
            return True
        await asyncio.sleep(SLOT_POLL_INTERVAL_SECONDS)

# Keep strong references to in-flight background tasks.
_BACKGROUND_RUNS: set[asyncio.Task] = set()

# Execution leases identify a live worker across replicas.
INSTANCE_ID = uuid.uuid4().hex

# Lease heartbeat interval and expiry threshold.
HEARTBEAT_INTERVAL_SECONDS = max(5, int(os.environ.get("ARENA_HEARTBEAT_INTERVAL_SECONDS", "15")))
HEARTBEAT_STALE_AFTER_SECONDS = max(
    HEARTBEAT_INTERVAL_SECONDS * 3, int(os.environ.get("ARENA_HEARTBEAT_STALE_AFTER_SECONDS", "90"))
)
# Periodically reclaim records whose leases have expired.
RECONCILE_INTERVAL_SECONDS = max(30, int(os.environ.get("ARENA_RECONCILE_INTERVAL_SECONDS", "60")))


def new_lease_fields() -> dict:
    """Return lease fields for work owned by this process."""
    return {"owner_instance_id": INSTANCE_ID, "heartbeat_at": _utcnow()}


def stale_lease_filter(now: dt.datetime | None = None) -> dict:
    """Match records with expired or missing execution leases."""
    cutoff = (now or _utcnow()) - dt.timedelta(seconds=HEARTBEAT_STALE_AFTER_SECONDS)
    return {
        "$or": [
            {"heartbeat_at": {"$lt": cutoff}},
            {"heartbeat_at": None},
            {"heartbeat_at": {"$exists": False}},
        ]
    }


@contextlib.asynccontextmanager
async def hold_lease(db: Database, collection: str, doc_id):
    """Keep `doc_id`'s lease fresh for as long as the wrapped work runs.

    Only ever writes the two lease fields, never `status` — so a row that
    something else legitimately finished or stopped is not resurrected by a
    heartbeat that happens to land afterwards.
    """
    coll = db[collection]
    coll.update_one({"_id": doc_id}, {"$set": new_lease_fields()})
    done = asyncio.Event()

    async def beat() -> None:
        while True:
            try:
                await asyncio.wait_for(done.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
                return  # released normally
            except asyncio.TimeoutError:
                pass
            try:
                coll.update_one({"_id": doc_id}, {"$set": new_lease_fields()})
            except Exception:
                # A transient Mongo blip must not kill the work itself; the
                # next beat retries, and the stale window is wide enough to
                # absorb several misses before anything reclaims the row.
                pass

    beater = asyncio.create_task(beat())
    try:
        yield
    finally:
        done.set()
        beater.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await beater

# How much of an adapter's stdout tail survives into the run document (see
# _execute_one) — admin-only visibility, so this stays generous rather than
# clipping the diagnostics that make it worth having in the first place.
RAW_LOG_MAX_CHARS = 20_000
# Persisting one Mongo update per streamed token is needlessly expensive.
# This makes the Admin Runs screen feel live while bounding database writes.
LIVE_LOG_FLUSH_INTERVAL_SECONDS = 1.0


def reconcile_orphaned_runs(db: Database) -> int:
    """Fails every run whose execution lease has gone stale.

    A run only ever progresses via the in-process asyncio task spawned by
    `run_task`/`retry_existing_run` — there is no persistent worker/queue
    behind it. If the backend process exits (crash, redeploy, manual
    restart) while a run is mid-flight, that task and its adapter
    subprocess die with it, but the run's Mongo document has no one left to
    ever finish or fail it: it sits at `pending`/`running` forever, with
    `deliverables_done` frozen at whatever it last polled to (which can
    misleadingly read as "6/6 done" while doing nothing — see the run #71
    incident this was added for).

    "Orphaned" is decided by the row's own lease (see hold_lease), never by
    this process's own age. The earlier version assumed a single backend
    process and failed every `pending`/`running` row it found at startup —
    which silently destroyed live work as soon as the platform's autoscaler
    ran a second replica, since a starting replica cannot distinguish
    another replica's in-flight run from a genuine leftover. Leases make
    that distinction explicit and are safe to evaluate from any number of
    replicas concurrently: a row is only claimed once nobody has refreshed
    it for HEARTBEAT_STALE_AFTER_SECONDS.
    """
    orphaned_ids = [
        r["_id"]
        for r in db.runs.find(
            {"status": {"$in": ["pending", "running"]}, **stale_lease_filter()}, {"_id": 1}
        )
    ]
    if not orphaned_ids:
        return 0
    # Whatever bytes were harvested before the crash are necessarily
    # incomplete (the adapter never reached its own success/failure
    # decision) — same reasoning `retry_run` uses to clear deliverables
    # before re-executing, applied here since these runs will never get
    # that retry-triggered cleanup on their own otherwise.
    db.deliverables.delete_many({"run_id": {"$in": orphaned_ids}})
    # Re-assert the whole condition in the write filter, not just the ids
    # gathered above: a run can legitimately finish (or be stopped, or have
    # its lease refreshed by an owner that was merely slow) in the gap
    # between that read and this write, and must not be dragged back to
    # "error" afterwards.
    result = db.runs.update_many(
        {
            "_id": {"$in": orphaned_ids},
            "status": {"$in": ["pending", "running"]},
            **stale_lease_filter(),
        },
        {
            "$set": {
                "status": "error",
                # Admin-only detail (masked to "Run failed." for anyone else
                # by routers/runs.py's _public_error_message). Deliberately
                # doesn't say "server restart" — the same lease-expiry path
                # now also covers a killed replica during an autoscale event
                # and a process that simply never refreshed its heartbeat,
                # neither of which is a restart.
                "error_message": "Lost its executor while running — the process driving it stopped "
                "renewing its lease, so no result was ever produced.",
                "finished_at": _utcnow(),
                "is_retrying": False,
            }
        },
    )
    return result.modified_count


def model_fingerprint(model: str, base_url: str) -> str:
    """Two provider profiles that point at the identical model+endpoint are
    "the same model" for caching/regenerate purposes, even if they're
    different named profiles (e.g. two personal keys for the same DeepSeek
    endpoint, or a personal profile that happens to match the free OnDemand
    one). Case/whitespace-insensitive since these are hand-typed fields."""
    return f"{(model or '').strip().lower()}|{(base_url or '').strip().lower()}"


def _normalize_model_label(text: str) -> str:
    return "".join(ch for ch in (text or "").strip().lower() if ch.isalnum())


def models_roughly_match(a: str, b: str) -> bool:
    """OnDemand's admin-curated model `label` (e.g. "DeepSeek V4 Flash") is
    meant to read like "the same underlying model" as a provider profile's
    `model` field (e.g. "deepseek-v4-flash"), but the two live in different
    naming conventions — so this is a loose, order-independent substring
    check on the alphanumeric-only, lowercased form of each, not an
    exact-equality one. Empty on either side never matches (nothing to
    compare)."""
    norm_a, norm_b = _normalize_model_label(a), _normalize_model_label(b)
    if not norm_a or not norm_b:
        return False
    return norm_a in norm_b or norm_b in norm_a


def _guess_media_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return {
        ".json": "application/json",
        ".csv": "text/csv",
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".html": "text/html",
        ".pdf": "application/pdf",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".py": "text/x-python",
        # Web-project sources (see webproject.py). Served as text/* so a
        # judge reading the code in-browser gets it rendered rather than
        # downloaded — these are read as source, never executed by us.
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".cjs": "text/javascript",
        ".jsx": "text/jsx",
        ".ts": "text/typescript",
        ".tsx": "text/tsx",
        ".css": "text/css",
        ".scss": "text/x-scss",
        ".vue": "text/plain",
        ".svelte": "text/plain",
        ".svg": "image/svg+xml",
        ".yml": "text/yaml",
        ".yaml": "text/yaml",
        ".toml": "text/plain",
    }.get(ext, "application/octet-stream")


def _task_entity(doc: dict) -> types.SimpleNamespace:
    """Adapters address the task via attribute access (task.title,
    task.prompt, ...) — a SimpleNamespace over the Mongo document gives them
    that without a dedicated dataclass to keep in sync with the document
    shape."""
    return types.SimpleNamespace(**doc)


def _resolve_provider_doc(db: Database, config_id: int | None) -> dict | None:
    """Shared lookup behind get_provider_settings and
    resolve_ondemand_model_id: the requested profile if given (every
    current caller always passes one — a battle picks its model
    explicitly, there's no separate "active profile" concept to fall back
    to), else the most recently created one (only reachable for a caller
    that omits config_id entirely, e.g. a stale request predating a
    profile's existence)."""
    return (
        (db.provider_config.find_one({"_id": config_id}) if config_id is not None else None)
        or db.provider_config.find_one(sort=[("_id", -1)])
    )


def get_provider_settings(db: Database, config_id: int | None = None) -> ProviderSettings:
    """The one active profile, used by every harness in a battle. Falls back
    to the most recently updated profile if none is flagged active
    (possible for rows created before profiles were named), and to empty
    settings if none exist at all."""
    cfg = _resolve_provider_doc(db, config_id)
    if cfg is None:
        return ProviderSettings(model="", base_url="", api_key="")
    free = bool(cfg.get("is_free") or cfg.get("is_shared"))
    return ProviderSettings(
        model=cfg.get("model", "").strip(),
        # A stray trailing/leading space here is invisible in the admin
        # form but breaks every harness that appends a path onto this
        # (e.g. opencode_cli.py's f"{base_url}/chat/completions" becomes
        # ".../v1 /chat/completions", a real 404 to the provider). Stripped
        # at the one place every harness's ProviderSettings comes from, so
        # this can't resurface even for a config row saved before
        # schemas.ProviderConfigIn started stripping on write.
        base_url=cfg.get("base_url", "").strip(),
        api_key=cfg.get("api_key", ""),
        # A free profile defaults to "low" effort when the admin never set
        # one explicitly; a personal profile gets no arena-imposed default
        # at all. See routers/config.py's effective_reasoning_effort.
        reasoning_effort=effective_reasoning_effort(cfg.get("reasoning_effort", ""), free),
    )


def resolve_ondemand_model_id(db: Database, config_id: int | None) -> int | None:
    """The admin-preset OnDemand model mapped to the shared free profile
    used in this battle (provider_config.ondemand_model_id — see
    routers/config.py). Replaces the old flow where the caller picked an
    OnDemand model by hand and the two were fuzzy-matched for consistency
    (runs.py's old models_roughly_match check) — the mapping makes them
    consistent by construction, so there's nothing left to mismatch."""
    cfg = _resolve_provider_doc(db, config_id)
    return (cfg or {}).get("ondemand_model_id")


def latest_runs_by_harness(db: Database, task_id: str, status: str | None = None, provider_config_id: int | None = None) -> dict[str, dict]:
    """The single "current" run per harness for a task — i.e. what caching,
    the judging view, and scoring should all treat as *the* result for that
    harness, ignoring older runs left behind by a Regenerate. A task can
    have many run documents per harness over time (every Regenerate adds
    one, deliberately, as a history/audit trail — see run_task) but only
    the most recent one should ever be user-visible as "the" result.

    Runs are ordered by id descending (not started_at/finished_at: a
    pending/running run may have no finished_at yet, and id order is a
    simpler, always-defined proxy for "most recently created") and the
    first one seen per harness_key wins.
    """
    query = {"task_id": task_id, "is_deleted": {"$ne": True}}
    if provider_config_id is not None:
        query["provider_config_id"] = provider_config_id
    if status is not None:
        query["status"] = status
    latest: dict[str, dict] = {}
    for run in db.runs.find(query).sort("_id", -1):
        latest.setdefault(run["harness_key"], run)
    return latest


def all_runs_for_task(db: Database, task_id: str, provider_config_id: int | None = None) -> list[dict]:
    """Every run document for this task — the full audit trail
    `latest_runs_by_harness` deliberately hides, so Battle Log can show every
    attempt (every Regenerate, every model tried), not just the current one
    per harness. Newest first."""
    query = {"task_id": task_id, "is_deleted": {"$ne": True}}
    if provider_config_id is not None:
        query["provider_config_id"] = provider_config_id
    return list(db.runs.find(query).sort("_id", -1))


async def _execute_one(run_id: int, task_id: str, harness_key: str, provider: ProviderSettings) -> None:
    """Runs against its own MongoClient-backed handle; pymongo's client is
    thread-safe/connection-pooled, so unlike the old SQLAlchemy Session this
    doesn't strictly need isolation per coroutine — kept as its own `db`
    handle anyway for symmetry with how this function is reasoned about."""
    db = get_client()[MONGODB_DB_NAME]
    async with hold_lease(db, "runs", run_id):
        try:
            await _execute_one_leased(db, run_id, task_id, harness_key, provider)
        except Exception as exc:
            log.error("run %s crashed outside adapter handling: %r", run_id, exc, exc_info=True)
            log_error(db, action="RUN_EXECUTION", message=f"run {run_id} crashed outside adapter handling", error=exc, metadata={"run_id": run_id, "task_id": task_id, "harness_key": harness_key})
            try:
                db.runs.update_one(
                    {"_id": run_id},
                    {
                        "$set": {
                            "status": "error",
                            "error_message": f"Internal error: {exc}",
                            "finished_at": _utcnow(),
                            "is_retrying": False,
                        }
                    },
                )
            except Exception:
                pass  # best-effort — the reconciler is still the backstop


async def _execute_one_leased(
    db: Database, run_id: int, task_id: str, harness_key: str, provider: ProviderSettings
) -> None:
    """The actual work, with this process's lease held for its whole
    duration (see `_execute_one`) — including the time spent queued on
    `_run_slots`, so a run waiting for a free execution slot is never
    mistaken for an abandoned one."""
    # `provider` is the SAME object for every harness in a battle (shared by
    # `prepare_runs`/`execute_prepared_runs`, which run every harness of a
    # battle concurrently via asyncio.gather over one ProviderSettings
    # instance). The callback fields set below are inherently per-run, so
    # mutating the shared instance directly would let concurrently-running
    # harnesses stomp on each other's callbacks — whichever harness's
    # assignment runs last would silently receive every other harness's
    # live log chunks for the rest of the battle. A shallow per-run copy
    # keeps everything else (model/base_url/api_key/...) shared as before,
    # while giving each harness its own place to hang its own callbacks.
    provider = dataclasses.replace(provider)
    workdir = tempfile.mkdtemp(prefix=f"harness-run-{run_id}-")
    # OnDemand's own harness sends selected skills straight to OnDemand's
    # chat query API instead (see harnesses/ondemand.py's `skillNames`) — its
    # backend fetches and injects the skill itself, so extracting the zip
    # into this workdir here would be redundant (and inert: this harness
    # never reads its own workdir as skill context).
    if provider.ondemand_skill_ids and harness_key != "ondemand":
        provider.workdir_skill_names = await download_and_extract_skills(
            workdir, provider.ondemand_api_key, provider.ondemand_skill_ids
        )

    def stop_requested() -> bool:
        doc = db.runs.find_one({"_id": run_id}, {"stop_requested": 1})
        return bool((doc or {}).get("stop_requested"))

    def mark_stopped() -> None:
        db.runs.update_one(
            {"_id": run_id},
            {"$set": {"status": "stopped", "finished_at": _utcnow(), "error_message": "Stopped by arena admin.", "is_retrying": False}},
        )
        cache_invalidate("runs_board")

    try:
        task_doc = db.tasks.find_one({"_id": task_id})
        # Actual reference-file bytes, if any were attached separately (see
        # routers/tasks.py's reference-files endpoints) — kept in their own
        # collection rather than on the task document itself, same reasoning
        # as deliverables living apart from runs. Each harness adapter
        # decides for itself what to do with `reference_file_blobs` (see
        # harnesses/_reference_files.py); most tasks have none.
        reference_blobs = list(db.task_reference_files.find({"task_id": task_id}))
        if reference_blobs:
            task_doc = {**task_doc, "reference_file_blobs": reference_blobs}
        task = _task_entity(task_doc)

        expected = parse_deliverables(task_doc.get("expected_deliverables", ""))
        db.runs.update_one(
            {"_id": run_id},
            {"$set": {"deliverables_done": 0, "deliverables_expected": len(expected)}},
        )

        adapter = get_adapter(db, harness_key)

        # Every harness streams its output incrementally now (OnDemand via
        # its provider's own SSE, the CLI-spawn adapters via their
        # subprocess's stdout/stderr — see harnesses/_collect.py). Keep only
        # an admin-only rolling tail in the run row so refreshing the
        # monitor (or a worker crash) never loses the diagnostics seen so
        # far. Every adapter scrubs a chunk before this callback receives it.
        live_log_parts: list[str] = []
        live_log_length = 0
        last_live_log_flush = 0.0

        def persist_live_log(chunk: str) -> None:
            nonlocal live_log_length, last_live_log_flush
            if not chunk:
                return
            live_log_parts.append(chunk)
            live_log_length += len(chunk)
            while live_log_length > RAW_LOG_MAX_CHARS and live_log_parts:
                removed = live_log_parts.pop(0)
                live_log_length -= len(removed)

            now = time.monotonic()
            if now - last_live_log_flush < LIVE_LOG_FLUSH_INTERVAL_SECONDS:
                return
            db.runs.update_one(
                {"_id": run_id, "status": "running"},
                {"$set": {"raw_log": "".join(live_log_parts)[-RAW_LOG_MAX_CHARS:]}},
            )
            last_live_log_flush = now

        provider.live_log_callback = persist_live_log
        if harness_key == "ondemand":
            # Persist immediately after session creation, not only when the
            # adapter eventually returns. A server restart can interrupt the
            # still-running query, but the admin can still inspect its
            # OnDemand session id on the resulting failed run.
            provider.ondemand_session_callback = lambda session_id: db.runs.update_one(
                {"_id": run_id},
                {
                    "$set": {"ondemand_session_id": session_id},
                    "$addToSet": {"ondemand_session_ids": session_id},
                },
            )
            # OnDemand sends answer deltas over its own provider SSE, not
            # through _collect.py's subprocess pump — feed the same throttled
            # writer as every other harness above.
            provider.ondemand_log_callback = persist_live_log
        try:
            if stop_requested():
                mark_stopped()
                return
            # Wait here for a free execution slot — first this process's own
            # (MAX_CONCURRENT_RUNS), then the fleet-wide one
            # (GLOBAL_MAX_CONCURRENT_RUNS, see _acquire_global_slot). The run
            # stays `pending` while queued at either gate and only flips to
            # `running` once it genuinely starts, so Battle Log's "in
            # progress" never claims work that hasn't begun.
            async with _run_slots:
                if not await _acquire_global_slot(db, stop_requested):
                    mark_stopped()
                    return
                if stop_requested():
                    mark_stopped()
                    return
                db.runs.update_one({"_id": run_id}, {"$set": {"status": "running", "started_at": _utcnow()}})
                # While a real adapter works, poll its isolated staging directory
                # for the task's expected files. Only the count reaches Battle
                # Log; helper files written by an adapter must not be shown as
                # deliverables. Bytes are still harvested atomically after
                # completion, so Evaluate can never expose a half-written file.
                adapter_task = asyncio.create_task(adapter.run(task, workdir, provider))
                last_progress = -1
                while not adapter_task.done():
                    if stop_requested():
                        adapter_task.cancel()
                        try:
                            await adapter_task
                        except asyncio.CancelledError:
                            pass
                        mark_stopped()
                        return
                    if expected:
                        progress = sum(
                            1
                            for relpath in expected
                            if os.path.isfile(os.path.join(workdir, relpath))
                            and os.path.getsize(os.path.join(workdir, relpath)) > 0
                        )
                    else:
                        progress = sum(len(files) for _, _, files in os.walk(workdir))
                    if progress != last_progress:
                        db.runs.update_one({"_id": run_id}, {"$set": {"deliverables_done": progress}})
                        last_progress = progress
                    try:
                        await asyncio.wait_for(asyncio.shield(adapter_task), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                result = await adapter_task
        except Exception as exc:  # adapter-level crash, not a task-level "error_message"
            result = None
            crash_message = str(exc)
        else:
            crash_message = None

        if crash_message is not None:
            db.runs.update_one(
                {"_id": run_id},
                {"$set": {"status": "error", "error_message": crash_message, "finished_at": _utcnow(), "is_retrying": False}},
            )
            cache_invalidate("runs_board")
            return

        # A truncated tail of adapter output, kept per-run so the admin can
        # see what a harness was actually doing without needing host/DB
        # access — see routers/runs.py's admin overview endpoint. Adapters
        # already scrub secrets and cap length before returning this; this
        # cap is a second, defensive one so nothing ever grows unbounded.
        raw_log = (result.raw_log or "")[-RAW_LOG_MAX_CHARS:]
        ondemand_session_id = (result.ondemand_session_id or "") if harness_key == "ondemand" else ""

        # `result.ok` is each adapter's own opinion, and not every adapter
        # guards against reporting success with nothing to show for it (see
        # harnesses/webhook.py — a custom harness the operator controls,
        # not something this app can fully trust to always self-check).
        # This central check catches that regardless of which adapter it
        # came from, instead of relying on each one to remember it.
        if result.ok and not result.deliverables:
            result = RunResult(
                ok=False,
                deliverables=[],
                raw_log=result.raw_log,
                error_message="Harness reported success but produced no deliverable files.",
                ondemand_session_id=result.ondemand_session_id,
            )

        if not result.ok:
            db.runs.update_one(
                {"_id": run_id},
                {
                    "$set": {
                        "status": "error",
                        "error_message": result.error_message or "harness run failed",
                        "finished_at": _utcnow(),
                        "raw_log": raw_log,
                        "ondemand_session_id": ondemand_session_id,
                        "is_retrying": False,
                    }
                },
            )
            cache_invalidate("runs_board")
            return

        written_count = 0
        oversized: list[str] = []
        for relpath in result.deliverables:
            abspath = os.path.join(workdir, relpath)
            if not os.path.isfile(abspath):
                continue
            # Deliverable bytes are stored inline on the document, and Mongo
            # rejects any document over 16MB outright (DocumentTooLarge) —
            # without this check, one oversized harness output would crash
            # run processing instead of just failing to save that one file.
            # Checked via stat before reading so we don't pull a huge file
            # into memory just to reject it.
            if os.path.getsize(abspath) > MAX_DELIVERABLE_BYTES:
                oversized.append(os.path.basename(relpath))
                log.warning(
                    "run %s: deliverable %s is %d bytes, over the %d byte cap — skipping",
                    run_id, relpath, os.path.getsize(abspath), MAX_DELIVERABLE_BYTES,
                )
                continue
            with open(abspath, "rb") as f:
                content = f.read()
            filename = os.path.basename(relpath)
            db.deliverables.insert_one(
                {
                    "_id": next_id(db, "deliverables"),
                    "run_id": run_id,
                    "filename": filename,
                    "relpath": relpath,
                    "media_type": _guess_media_type(relpath),
                    "size_bytes": len(content),
                    "content": content,
                }
            )
            written_count += 1

        # Every claimed filename could still turn out missing from disk
        # (the loop above just skips those) — `result.ok`/non-empty
        # `result.deliverables` isn't proof anything was actually written
        # to Mongo. Same "no deliverables" failure as the check above, just
        # caught one step later.
        if written_count == 0:
            error_message = "Harness reported success but produced no deliverable files."
            if oversized:
                error_message = (
                    f"Harness produced only oversized deliverable file(s) ({', '.join(oversized)}) "
                    f"exceeding the {MAX_DELIVERABLE_BYTES // (1024 * 1024)}MB per-file storage limit."
                )
            db.runs.update_one(
                {"_id": run_id},
                {
                    "$set": {
                        "status": "error",
                        "error_message": error_message,
                        "finished_at": _utcnow(),
                        "raw_log": raw_log,
                        "ondemand_session_id": ondemand_session_id,
                        "is_retrying": False,
                    }
                },
            )
            cache_invalidate("runs_board")
            return

        # A harness that deployed the app itself (OnDemand for web tasks)
        # hands back a live URL. Recorded as this run's deployment so the
        # preview endpoint serves it instead of paying to build the same
        # code again; once it expires, sandbox_deploy falls back to
        # redeploying from the deliverables stored above.
        deployment_fields = {}
        if result.preview_url:
            # The harness's sandbox has its own lifetime that nothing
            # calls back to report, so record when it lapses. Without an
            # expires_at the row would read "live" forever and the UI
            # would keep pointing at a dead URL instead of offering a
            # redeploy (see sandbox_deploy.deployment_state). OnDemand's
            # observed limit is 90 minutes, matching our own default.
            deployment_fields = {
                "deployment.status": "live",
                "deployment.preview_url": result.preview_url,
                "deployment.sandbox_id": result.preview_sandbox_id,
                "deployment.provider": "harness",
                "deployment.expires_at": _utcnow() + dt.timedelta(seconds=HARNESS_PREVIEW_TTL_SECONDS),
                "deployment.error": "",
                "deployment.updated_at": _utcnow(),
            }

        db.runs.update_one(
            {"_id": run_id},
            {
                "$set": {
                    **deployment_fields,
                    "status": "done",
                    "finished_at": _utcnow(),
                    # A retry may be succeeding over an earlier failure.
                    # Never leave that stale failure visible on a completed
                    # run (or make Admin Runs offer Retry for a done run).
                    "error_message": "",
                    "deliverables_done": written_count,
                    "deliverables_expected": len(expected),
                    "raw_log": raw_log,
                    "is_retrying": False,
                    "ondemand_session_id": ondemand_session_id,
                }
            },
        )
        cache_invalidate("stats", "leaderboard", "runs_board")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def retry_existing_run(run_id: int, user_id: int | None = None) -> None:
    """Execute an already-reset failed run in place for Battle Log retry."""
    db = get_client()[MONGODB_DB_NAME]
    run = db.runs.find_one({"_id": run_id})
    if run is None:
        return
    provider = get_provider_settings(db, run.get("provider_config_id"))
    skill_ids = run.get("skill_ids") or []
    provider.ondemand_skill_ids = skill_ids
    if (skill_ids or run["harness_key"] == "ondemand") and user_id is not None:
        user_doc = db.users.find_one({"_id": user_id})
        provider.ondemand_api_key = (user_doc or {}).get("ondemand_api_key") or ""
    if run["harness_key"] == "ondemand":
        model_doc = db.ondemand_models.find_one({"_id": run.get("ondemand_model_id")})
        provider.ondemand_endpoint_id = (model_doc or {}).get("endpoint_id") or ""
        provider.ondemand_reasoning_effort = (model_doc or {}).get("reasoning_effort") or ""
        provider.ondemand_suggest_plugins_enabled = suggest_plugins_enabled(db)
    await _execute_one(run_id, run["task_id"], run["harness_key"], provider)


def prepare_runs(
    db: Database,
    task_id: str,
    harness_keys: list[str],
    force: bool = False,
    provider_config_id: int | None = None,
    user_id: int | None = None,
    ondemand_model_id: int | None = None,
    skill_ids: list[str] | None = None,
    skill_names: list[str] | None = None,
) -> tuple[list[int], list[tuple[int, str]], ProviderSettings]:
    """Insert this battle's run rows (already leased to this process) and
    return what's needed to execute them, without executing anything yet.

    Split out from `run_task` so a caller can hand the rows straight back to
    the user and execute in the background — see `start_runs`.
    """
    task = db.tasks.find_one({"_id": task_id})
    if task is None:
        raise ValueError(f"unknown task: {task_id}")

    provider = get_provider_settings(db, provider_config_id)
    # OnDemand doesn't use the shared profile above at all — see base.py's
    # ProviderSettings and harnesses/ondemand.py. Filled in here (rather
    # than inside the adapter) because only the orchestration layer knows
    # which user is running this battle and which admin-curated model they
    # picked; the validation that these actually exist and match the other
    # harnesses' model lives in the router, before this function is ever
    # called (routers/runs.py, routers/batches.py).
    ondemand_model_label = ""
    # Selected skills come from the user's OnDemand account regardless of
    # which harnesses are actually running this battle, so their API key is
    # needed here even when "ondemand" itself isn't one of harness_keys.
    if skill_ids and "ondemand" not in harness_keys and user_id is not None:
        user_doc = db.users.find_one({"_id": user_id})
        provider.ondemand_api_key = (user_doc or {}).get("ondemand_api_key") or ""
    provider.ondemand_skill_ids = skill_ids or []
    if "ondemand" in harness_keys:
        if user_id is not None:
            user_doc = db.users.find_one({"_id": user_id})
            provider.ondemand_api_key = (user_doc or {}).get("ondemand_api_key") or ""
        if ondemand_model_id is not None:
            model_doc = db.ondemand_models.find_one({"_id": ondemand_model_id})
            provider.ondemand_endpoint_id = (model_doc or {}).get("endpoint_id") or ""
            provider.ondemand_reasoning_effort = (model_doc or {}).get("reasoning_effort") or ""
            ondemand_model_label = (model_doc or {}).get("label") or ""
        provider.ondemand_suggest_plugins_enabled = suggest_plugins_enabled(db)
    fingerprint = model_fingerprint(provider.model, provider.base_url)
    # One id shared by every run this call creates — "the whole battle" as
    # a single thing to reference, since a battle is N separate run
    # documents (one per harness) with their own individually-incrementing
    # ids. A non-sequential UUID prevents the public Battle Log from
    # revealing how many battles have been triggered. Persists across a Retry (retry_run only $sets specific
    # fields on the existing run document, never touches round_id) — a
    # recovered run stays part of the same round it always was.
    round_id = uuid.uuid4().hex
    run_ids: list[int] = []
    reused_same_model: list[str] = []
    to_execute: list[tuple[int, str]] = []
    for harness_key in harness_keys:
        adapter = get_adapter(db, harness_key)  # raises KeyError early if unknown

        # Keep OnDemand's endpoint id internal; the user-facing run model is
        # the admin-curated label (for example, "Deepseek V4 Flash").
        # Fingerprinting still uses the endpoint id so execution identity is
        # never confused with a display label.
        if harness_key == "ondemand":
            harness_model = ondemand_model_label or provider.ondemand_endpoint_id
            harness_fingerprint = model_fingerprint(provider.ondemand_endpoint_id, "ondemand")
        else:
            harness_model = provider.model
            harness_fingerprint = fingerprint

        run_id = next_id(db, "runs")
        db.runs.insert_one(
            {
                "_id": run_id,
                "round_id": round_id,
                "task_id": task_id,
                "harness_key": harness_key,
                "provider_config_id": provider_config_id,
                # Stored per-run (not just passed through) so a later Retry
                # (see retry_existing_run) can reapply the same skills.
                "skill_ids": skill_ids or [],
                # Display-only — see schemas.RunRequest.skill_names.
                "skill_names": skill_names or [],
                # Only meaningful for the "ondemand" harness_key — stored
                # per-run (not just passed through) so a later Retry (see
                # routers/runs.py::retry_run, which only knows this one run
                # document) can resolve the same OnDemand model again.
                "ondemand_model_id": ondemand_model_id if harness_key == "ondemand" else None,
                "model": harness_model,
                "model_fingerprint": harness_fingerprint,
                # Who triggered this run — the only person (besides an
                # admin) allowed to retry it if it fails. Absent on runs
                # created before this existed, which is why every read of
                # it uses .get() and treats missing as "unknown submitter".
                "submitted_by_user_id": user_id,
                "status": "pending",
                "started_at": None,
                "finished_at": None,
                "error_message": "",
                "deliverables_done": 0,
                "deliverables_expected": 0,
                "source": "live",
                "stop_requested": False,
                "created_at": _utcnow(),
                # Leased to this process from the moment the row exists, so
                # a peer replica's reconciliation can never mistake a row
                # created milliseconds ago for an abandoned one.
                **new_lease_fields(),
            }
        )
        run_ids.append(run_id)
        to_execute.append((run_id, harness_key))

    cache_invalidate("runs_board")
    return run_ids, to_execute, provider


async def execute_prepared_runs(
    task_id: str, to_execute: list[tuple[int, str]], provider: ProviderSettings
) -> None:
    """Run every harness of one prepared battle concurrently."""
    if to_execute:
        await asyncio.gather(*[_execute_one(rid, task_id, hkey, provider) for rid, hkey in to_execute])


async def run_task(
    db: Database,
    task_id: str,
    harness_keys: list[str],
    force: bool = False,
    provider_config_id: int | None = None,
    user_id: int | None = None,
    ondemand_model_id: int | None = None,
    skill_ids: list[str] | None = None,
    skill_names: list[str] | None = None,
) -> tuple[list[int], list[str]]:
    """Create fresh runs for every selected harness and execute them,
    returning only once every harness has finished.

    Used where the caller genuinely needs to wait — batches.py runs tasks
    strictly one at a time, so that each finished task's deliverables become
    gradable while the rest of the batch is still working. An interactive
    battle trigger should use `start_runs` instead.

    Historical result reuse is deliberately disabled for now: selecting a
    task in Benchmark always creates a new live result, including when that
    harness/model completed the same task previously.
    """
    run_ids, to_execute, provider = prepare_runs(
        db,
        task_id,
        harness_keys,
        force=force,
        provider_config_id=provider_config_id,
        user_id=user_id,
        ondemand_model_id=ondemand_model_id,
        skill_ids=skill_ids,
        skill_names=skill_names,
    )
    await execute_prepared_runs(task_id, to_execute, provider)
    return run_ids, []


def start_runs(
    db: Database,
    task_id: str,
    harness_keys: list[str],
    force: bool = False,
    provider_config_id: int | None = None,
    user_id: int | None = None,
    ondemand_model_id: int | None = None,
    skill_ids: list[str] | None = None,
    skill_names: list[str] | None = None,
    on_complete: Callable[[], None] | None = None,
) -> tuple[list[int], list[str]]:
    """Create this battle's runs and execute them in the BACKGROUND,
    returning the (still `pending`) run ids immediately.

    A battle can legitimately take up to ARENA_HARNESS_TIMEOUT_SECONDS per
    harness, so awaiting it inside the HTTP request meant holding that
    request open for up to an hour — long enough for gateways to time it
    out, for a client disconnect to look like a failure, and for the whole
    battle to die with whichever replica happened to serve it. The UI never
    needed the wait: it ignores this response's body and polls run status
    anyway (Battle Log / Evaluate both poll).

    `on_complete`, if given, runs once every harness has finished (whatever
    their individual outcomes — a harness failing is already handled inside
    `_execute_one` and doesn't raise here) — NOT when the run rows are
    created. `routers/runs.py` uses this to charge the submission quota only
    once the battle has actually happened, matching the old
    await-then-charge behavior from before execution was backgrounded,
    rather than charging for a battle that might still fail outright.
    Skipped if the battle itself crashes (see `_finished` below) — same as
    the old behavior, where an exception propagating out of the awaited
    call meant the charge after it never ran.

    Must be called from a running event loop — same requirement, and same
    `asyncio.create_task` pattern, as batches.start_batch.
    """
    run_ids, to_execute, provider = prepare_runs(
        db,
        task_id,
        harness_keys,
        force=force,
        provider_config_id=provider_config_id,
        user_id=user_id,
        ondemand_model_id=ondemand_model_id,
        skill_ids=skill_ids,
        skill_names=skill_names,
    )

    async def _run_then_complete() -> None:
        await execute_prepared_runs(task_id, to_execute, provider)
        if on_complete is not None:
            on_complete()

    task = asyncio.create_task(_run_then_complete())
    _BACKGROUND_RUNS.add(task)

    def _finished(finished: asyncio.Task) -> None:
        _BACKGROUND_RUNS.discard(finished)
        # Nothing awaits this task, so an exception escaping it would
        # otherwise be swallowed entirely (asyncio only complains once the
        # task is garbage collected). `_execute_one` already records
        # adapter-level failures on the run document itself; what lands here
        # is a bug *outside* that handling, which would leave the row sitting
        # `pending` until the reconciler eventually sweeps it — with a
        # "server restart" message that isn't what actually happened. Log it
        # so the real cause is recoverable from container logs.
        if finished.cancelled():
            return
        exc = finished.exception()
        if exc is not None:
            log.error("background battle for task %s crashed: %r", task_id, exc, exc_info=True)
            try:
                log_error(get_client()[MONGODB_DB_NAME], action="RUN_EXECUTION", message=f"background battle for task {task_id} crashed", error=exc, metadata={"task_id": task_id})
            except Exception:
                log.exception("failed to persist background-battle-crash log entry")

    task.add_done_callback(_finished)
    return run_ids, []



async def reconciliation_loop() -> None:
    """Periodically reclaim runs whose lease has gone stale.

    Startup-only reconciliation was sufficient while exactly one process
    could ever own a run: the process that died was always the process that
    came back. Once the platform can run several replicas, a replica that
    dies mid-run leaves a stale lease that no other replica's *startup* will
    ever look at — the run would sit "running" in the UI indefinitely, and
    (for its user) keep tripping require_no_active_runs. Sweeping on an
    interval closes that gap without needing a real job queue.

    Deliberately never raises: this is a janitor, and a bad sweep must not
    take down a process that is otherwise serving traffic fine.
    """
    from .batches import reconcile_orphaned_batches  # local: avoids an import cycle

    while True:
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
        try:
            db = get_client()[MONGODB_DB_NAME]
            runs = reconcile_orphaned_runs(db)
            batches = reconcile_orphaned_batches(db)
            if runs or batches:
                log.info("reclaimed %d abandoned run(s) and %d abandoned batch(es)", runs, batches)
        except Exception as exc:
            log.error("reconciliation sweep failed: %s", exc, exc_info=True)
            try:
                log_error(get_client()[MONGODB_DB_NAME], action="RECONCILIATION_SWEEP", message="reconciliation sweep failed", error=exc)
            except Exception:
                log.exception("failed to persist reconciliation-sweep-failure log entry")
