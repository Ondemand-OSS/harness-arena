"""Live previews for web-development runs, via Vercel Sandbox.

A web-development run (see webproject.is_web_project) produces source
that only means anything when it runs, so instead of asking a judge to
read `App.jsx` we start it in an ephemeral Firecracker microVM on Vercel
and hand back a public `https://sb-<random>.vercel.run` URL.

Deliberately a SANDBOX, never `vercel deploy`: the latter publishes a
real, persistent production deployment of arbitrary model-generated code
under the arena's own Vercel account. A sandbox is ephemeral,
self-expiring and isolated, which is the correct blast radius for running
untrusted output.

Uses Vercel's official Python SDK (`vercel.sandbox`) rather than the
`sandbox` Node CLI. Both drive the same API and produce the same
ephemeral sandboxes, but the SDK is a native async Python client — no
Node runtime to install alongside this service, no subprocess to spawn,
and no scraping structured data back out of CLI stdout. It reads the same
VERCEL_TOKEN / VERCEL_TEAM_ID / VERCEL_PROJECT_ID environment variables
the CLI does.

Nothing is deployed when a run finishes. Sandboxes bill for wall-clock
time and most runs are never previewed, so deployment is lazy: the first
person to ask for a preview triggers it (see ensure_preview), and when
that sandbox expires the next request transparently starts a fresh one.

Every failure path is non-fatal by design — if credentials are unset, npm
install fails, or the dev server never binds, the run simply has no
preview and the caller falls back to offering the zipped source (see
routers/deploy.py).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
import urllib.parse

from pymongo.database import Database

from .webproject import (
    detect_framework,
    find_frontend_root,
    find_missing_local_imports,
    patch_vite_allowed_hosts,
    safe_relpath,
)

log = logging.getLogger(__name__)

# Long enough that a preview opened for judging stays usable for a whole
# session, short enough that an abandoned one stops billing on its own.
SANDBOX_TIMEOUT_SECONDS = int(os.environ.get("ARENA_SANDBOX_TIMEOUT_SECONDS", str(90 * 60)))
# Vercel's default image already ships Node and npm, so no runtime choice
# is needed the way the CLI's --runtime flag required one.
SANDBOX_IMAGE = os.environ.get("ARENA_SANDBOX_IMAGE", "").strip() or None
# 1 vCPU (and the 2 GB that comes with it) instead of the platform default
# of 2. Provisioned memory bills for the sandbox's whole wall-clock life
# whether or not anything is happening, and a preview spends nearly all of
# that idle waiting to be looked at — so halving the allocation halves the
# dominant cost. A Vite/Next dev server serving one viewer does not need
# two cores; the only thing that gets slower is the initial npm install.
SANDBOX_VCPUS = int(os.environ.get("ARENA_SANDBOX_VCPUS", "1"))
SANDBOX_ROOT = "/vercel/sandbox"

INSTALL_TIMEOUT_SECONDS = 900
# How long to wait for the dev server to actually answer after being
# started. Vite is quick; a first-run Next.js compile is not.
READY_TIMEOUT_SECONDS = 180
READY_POLL_SECONDS = 2
# How long a `deploying` record is trusted before another request may
# take the slot over. Comfortably longer than the worst case above, so it
# only fires for a deploy whose worker actually died, never a slow one.
STALE_DEPLOY_AFTER_SECONDS = 30 * 60

STATUS_DEPLOYING = "deploying"
STATUS_LIVE = "live"
STATUS_FAILED = "failed"
# A preview that WAS live and whose sandbox has since self-terminated.
# Distinct from "failed" (which means the build broke) and from "idle"
# (never deployed): the project is fine, its host simply timed out, and
# the fix is one click rather than an error to interpret.
STATUS_EXPIRED = "expired"
# Prefix on a stored deployment.error that marks it as
# find_missing_local_imports' specific, deterministic reason — routers/deploy.py
# checks for this to show a precise message instead of the generic one
# every other (non-deterministic, possibly-transient) failure gets.
MISSING_FILES_PREFIX = "missing_files:"


def deployment_state(deployment: dict | None) -> str:
    """The status to SHOW for a stored deployment.

    Derived rather than stored, because expiry happens by the passage of
    time inside Vercel — nothing calls back to tell us a sandbox stopped,
    so a row would otherwise sit on `live` forever pointing at a dead
    URL. Compared against `expires_at` instead of probing the URL so the
    status endpoint stays a cheap read that the UI can poll.
    """
    deployment = deployment or {}
    status = deployment.get("status") or "idle"
    if status != STATUS_LIVE:
        return status
    expires_at = deployment.get("expires_at")
    if isinstance(expires_at, dt.datetime) and expires_at <= _utcnow():
        return STATUS_EXPIRED
    return STATUS_LIVE


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def sandbox_credentials() -> tuple[str, str, str]:
    return (
        os.environ.get("VERCEL_TOKEN", "").strip(),
        os.environ.get("VERCEL_TEAM_ID", "").strip(),
        os.environ.get("VERCEL_PROJECT_ID", "").strip(),
    )


def preview_unavailable_reason() -> str:
    """Why previews can't be produced at all right now, or '' when they
    can. Checked before any Mongo writes so a misconfigured deployment
    reports one clear reason instead of failing per-run."""
    try:
        import vercel.sandbox  # noqa: F401
    except ImportError:
        return "the Vercel Python SDK is not installed on the server (pip install vercel)"
    token, team, project = sandbox_credentials()
    missing = [
        name
        for name, value in (("VERCEL_TOKEN", token), ("VERCEL_TEAM_ID", team), ("VERCEL_PROJECT_ID", project))
        if not value
    ]
    if missing:
        return f"missing server configuration: {', '.join(missing)}"
    return ""


def _scrub(text: str) -> str:
    """Never let the Vercel token reach a log line or an error message
    stored on the run document."""
    token, team, _project = sandbox_credentials()
    for secret in (token, team):
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


# Matches a <script ... src="..."> tag, module or not — the module-typed
# form first since that's what Vite/modern bundlers emit and a plain
# `src=` attribute can appear before `type=` in the tag either way.
_MODULE_SCRIPT_RE = re.compile(r'<script[^>]+type=["\']module["\'][^>]*\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
_SCRIPT_SRC_RE = re.compile(r'<script[^>]*\bsrc=["\']([^"\']+)["\'][^>]*>', re.IGNORECASE)


def _entry_script_url(html: str, base_url: str) -> str | None:
    """First <script src> that isn't one of Vite's own injected dev-tooling
    scripts. Confirmed against a live sandbox: Vite's dev server injects
    `/@react-refresh` and `/@vite/client` at the top of `<head>` — always
    trivially servable, since they're Vite's own built-in client, not the
    project's code — before the app's real entry script appears later in
    `<body>` (e.g. `/src/main.jsx`). Taking the first match unconditionally
    always found Vite's own script and never the app's, making the
    readiness check below a no-op for exactly the framework this exists
    for. `/@id/`, `/@fs/`, `/@vite/`, `/@react-refresh` are all Vite's own
    virtual-module/dev-tooling path convention (the leading `@` is
    reserved for it), so skipping any src starting with `@` (after
    resolving a leading `/`) reliably leaves only real project paths.
    """
    for pattern in (_MODULE_SCRIPT_RE, _SCRIPT_SRC_RE):
        for match in pattern.finditer(html):
            src = match.group(1)
            if src.lstrip("/").startswith("@"):
                continue
            return urllib.parse.urljoin(base_url, src)
    return None


async def _url_is_live(url: str, timeout: float = 5.0) -> bool:
    """Whether `url` is actually servable — not just its root document.

    A plain 200 on `/` isn't enough for a Vite (or similar) dev server:
    the root index.html is served instantly, but the JS module it
    references is compiled on first request and can still 404/500 for a
    few seconds after that. Reporting "live" on the root check alone is
    exactly what let a judge's first preview load land blank — a manual
    reload worked purely because the module had finished warming up by
    then. Resolving and probing that entry script too means "live" means
    the app can actually render, not just that something answered.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            if not (200 <= resp.status_code < 400):
                return False
            entry = _entry_script_url(resp.text, str(resp.url))
            if entry is None:
                # Not an HTML document this recognizes (a static site, an
                # API route, ...) — the root check is all there is to do.
                return True
            entry_resp = await client.get(entry)
            return 200 <= entry_resp.status_code < 400
    except Exception:
        return False


def load_run_files(db: Database, run_id: int) -> dict[str, bytes]:
    """The run's deliverables as a {relative path: bytes} tree.

    Uses `relpath` (which preserves `src/App.jsx`) rather than `filename`,
    falling back to the flat filename for rows written before nested
    deliverables existed."""
    files: dict[str, bytes] = {}
    for doc in db.deliverables.find({"run_id": run_id, "is_deleted": {"$ne": True}}):
        content = doc.get("content")
        if content is None:
            continue
        # Paths come from harness output, so one that climbs out of the
        # project root is dropped here — before it can be uploaded into a
        # sandbox or written into a zip a user will extract locally.
        relpath = safe_relpath(doc.get("relpath") or doc.get("filename") or "")
        if not relpath:
            continue
        files[relpath] = bytes(content)
    return files


def _format_missing_imports(missing: list[tuple[str, str]]) -> str:
    """`missing` as `"./styles.css (imported by src/main.jsx), ..."` —
    shared by undeployable_reason and _deploy's own pre-sandbox check so
    the two always describe the same failure the same way."""
    return ", ".join(f"{spec} (imported by {src})" for src, spec in missing[:5])


def undeployable_reason(files: dict[str, bytes]) -> str:
    """Empty when `files` looks deployable; otherwise the raw
    "spec (imported by file), ..." fragment naming why not — currently
    just the missing-local-import check, but the one entry point
    routers/deploy.py's GET /preview calls to decide whether to offer
    "View website" at all (it wraps this fragment in its own judge-facing
    sentence — see that module's _missing_files_message). Cheap and
    synchronous (no sandbox, no network) by design, so it's fine to call
    on every status poll rather than only right before deploying."""
    root = find_frontend_root(files)
    project_files = _subtree(files, root)
    missing = find_missing_local_imports(project_files)
    return _format_missing_imports(missing) if missing else ""


def _set_deployment(db: Database, run_id: int, **fields) -> None:
    db.runs.update_one(
        {"_id": run_id},
        {"$set": {f"deployment.{key}": value for key, value in {**fields, "updated_at": _utcnow()}.items()}},
    )


def _subtree(files: dict[str, bytes], root: str) -> dict[str, bytes]:
    """Only the frontend's files, re-rooted so they land directly in the
    sandbox working directory (a `frontend/`-nested app must arrive as
    `package.json`, not `frontend/package.json`, or npm finds nothing)."""
    if not root:
        return files
    prefix = f"{root}/"
    return {path[len(prefix):]: content for path, content in files.items() if path.startswith(prefix)}


async def _deploy(db: Database, run_id: int, files: dict[str, bytes]) -> dict:
    """Create a sandbox, upload the frontend into it, start it, verify it.

    Returns the deployment sub-document that was persisted."""
    from vercel.sandbox import SandboxResources, create_sandbox

    async def fail(reason: str) -> dict:
        message = _scrub(reason)[:2000]
        log.warning("preview deploy failed for run %s: %s", run_id, message)
        _set_deployment(db, run_id, status=STATUS_FAILED, error=message, preview_url="", sandbox_id="")
        return {"status": STATUS_FAILED, "error": message}

    root = find_frontend_root(files)
    framework, port, start_command = detect_framework(files, root)
    project_files = patch_vite_allowed_hosts(_subtree(files, root), framework)
    if not project_files:
        return await fail("could not locate the frontend directory in this project")

    # Checked before a sandbox is ever created (same check GET /preview
    # already ran to decide whether to offer the button at all — see
    # undeployable_reason) — a project like this fails identically on
    # every retry, so there is nothing a sandbox (or another 3 minutes of
    # readiness polling) could tell us that this cheap, static check
    # doesn't already know. MISSING_FILES_PREFIX lets routers/deploy.py
    # show a specific, actionable reason instead of the generic message
    # every other (non-deterministic) failure gets.
    missing_imports = find_missing_local_imports(project_files)
    if missing_imports:
        return await fail(f"{MISSING_FILES_PREFIX}{_format_missing_imports(missing_imports)}")

    sandbox = None
    try:
        # `destroy=False`: the sandbox has to outlive this coroutine — it's
        # what the user is about to browse. It stops itself at
        # execution_time_limit instead.
        sandbox = await create_sandbox(
            ports=[port],
            execution_time_limit=dt.timedelta(seconds=SANDBOX_TIMEOUT_SECONDS),
            resources=SandboxResources(vcpus=SANDBOX_VCPUS),
            destroy=False,
            **({"image": SANDBOX_IMAGE} if SANDBOX_IMAGE else {}),
        )

        # The preview host is a random token assigned by the platform and
        # is not derivable from the sandbox id, so it's only ever read
        # from the route the platform hands back.
        route = next((r for r in (sandbox.routes or []) if r.port == port and not r.system), None)
        route = route or next((r for r in (sandbox.routes or []) if not r.system), None)
        if route is None or not route.url:
            return await fail("the sandbox exposed no public route for the app's port")
        preview_url = route.url
        sandbox_id = getattr(sandbox, "id", "") or getattr(sandbox, "name", "") or ""

        expires_at = _utcnow() + dt.timedelta(seconds=SANDBOX_TIMEOUT_SECONDS)
        _set_deployment(
            db,
            run_id,
            status=STATUS_DEPLOYING,
            sandbox_id=sandbox_id,
            preview_url=preview_url,
            port=port,
            framework=framework,
            expires_at=expires_at,
            error="",
        )

        # Upload project files in one filesystem batch.
        async with sandbox.fs.batch(cwd=SANDBOX_ROOT) as batch:
            for relpath, content in sorted(project_files.items()):
                batch.write_bytes(relpath, content)

        if framework != "static":
            install = await sandbox.run_process(
                "npm",
                ["install", "--no-audit", "--no-fund"],
                cwd=SANDBOX_ROOT,
                capture_output=True,
                kill_after=dt.timedelta(seconds=INSTALL_TIMEOUT_SECONDS),
            )
            if install.returncode != 0:
                tail = ((install.stderr or "") + (install.stdout or ""))[-1500:]
                return await fail(f"npm install failed in the sandbox: {tail}")

        # create_process (not run_process) because a dev server never
        # exits — awaiting it would hang until the sandbox expired.
        await sandbox.create_process(
            "bash",
            ["-lc", f"{start_command} > /tmp/dev.log 2>&1"],
            cwd=SANDBOX_ROOT,
        )

        # Spawning the process is not the same as it listening — poll the
        # public URL rather than handing back a URL that 502s.
        deadline = asyncio.get_event_loop().time() + READY_TIMEOUT_SECONDS
        while asyncio.get_event_loop().time() < deadline:
            if await _url_is_live(preview_url):
                _set_deployment(db, run_id, status=STATUS_LIVE, error="")
                log.info("preview live for run %s at %s", run_id, preview_url)
                return {"status": STATUS_LIVE, "preview_url": preview_url, "expires_at": expires_at}
            await asyncio.sleep(READY_POLL_SECONDS)

        devlog = ""
        try:
            tail = await sandbox.run_process("cat", ["/tmp/dev.log"], capture_output=True)
            devlog = (tail.stdout or "")[-1500:]
        except Exception:  # noqa: BLE001 - diagnostics only
            pass
        return await fail(f"the app never started responding. Dev server log tail: {devlog}")
    except Exception as exc:  # noqa: BLE001 - a preview must never break the run
        log.exception("unexpected error deploying preview for run %s", run_id)
        # The sandbox is only torn down on the failure path; a successful
        # one must stay up for the user to browse.
        if sandbox is not None:
            try:
                await sandbox.stop()
            except Exception:  # noqa: BLE001
                pass
        return await fail(f"unexpected error: {type(exc).__name__}: {exc}")


async def ensure_preview(db: Database, run_id: int, files: dict[str, bytes]) -> dict:
    """Return a live preview for this run, deploying only if needed.

    Reuses a sandbox that's still answering; redeploys transparently once
    it has expired (sandboxes self-terminate at their execution time
    limit, so an old URL is simply dead rather than wrong)."""
    run = db.runs.find_one({"_id": run_id}, {"deployment": 1})
    current = (run or {}).get("deployment") or {}

    # Covers both a sandbox we deployed and one the harness deployed
    # itself (provider == "harness"; OnDemand does this for web tasks and
    # the run already carries its URL — see runner.py). Either way the
    # question is the same: is it still answering? If not, we redeploy
    # from the stored files, which is exactly why the OnDemand adapter
    # copies the sources out of OnDemand's sandbox before it expires.
    if current.get("status") == STATUS_LIVE and current.get("preview_url"):
        if await _url_is_live(current["preview_url"]):
            return {
                "status": STATUS_LIVE,
                "preview_url": current["preview_url"],
                "expires_at": current.get("expires_at"),
                "provider": current.get("provider", "arena"),
            }

    # Claim the deploy slot atomically, so two people clicking "View
    # website" at the same moment can't start two billed sandboxes for one
    # run. The loser polls the same status document the winner is writing.
    #
    # A `deploying` record is only respected while it's FRESH. Without the
    # staleness clause, a worker killed mid-deploy (restart, autoscale,
    # crash) would leave the run pinned to `deploying` forever, and since
    # claiming requires "not currently deploying", nothing could ever
    # retry it — the preview would be permanently unreachable with no way
    # back short of editing Mongo. Same reasoning as runner.py's lease
    # heartbeats, at a much coarser granularity.
    stale_before = _utcnow() - dt.timedelta(seconds=STALE_DEPLOY_AFTER_SECONDS)
    claimed = db.runs.update_one(
        {
            "_id": run_id,
            "$or": [
                {"deployment.status": {"$ne": STATUS_DEPLOYING}},
                {"deployment.updated_at": {"$lt": stale_before}},
                {"deployment.updated_at": {"$exists": False}},
            ],
        },
        {"$set": {"deployment.status": STATUS_DEPLOYING, "deployment.error": "", "deployment.updated_at": _utcnow()}},
    )
    if claimed.modified_count != 1:
        return {"status": STATUS_DEPLOYING, "preview_url": "", "detail": "a deployment is already in progress"}

    return await _deploy(db, run_id, files)
