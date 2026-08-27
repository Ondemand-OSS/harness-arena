"""Recognizing web-development tasks and packaging their deliverables.

Most tasks produce documents (xlsx/docx/pdf) that a judge reads directly.
A web-development task instead produces *source files that only mean
something when they run together* — a React/Vite app, a Next.js page, a
plain index.html. Reading `App.jsx` as text tells a judge almost nothing
about whether the thing works, so those runs get a live preview instead
(see sandbox_deploy.py), with the zipped source as the fallback whenever
a preview can't be produced.

Detection is derived from the task's `expected_deliverables` rather than
a hand-set flag, so every task already in the dataset classifies itself
with no admin backfill (see routers/config.py's family field for the
opposite trade-off, where a fixed enum genuinely needed one).

Only the FRONTEND is ever deployed. A generated project may well include
a backend (`server/`, `api/`, a Dockerfile), but running untrusted server
code — with its own ports, env, and outbound calls — is a different risk
and lifecycle question than serving a static/dev-server frontend. The
backend files still ship inside the downloadable zip; they're just not
what gets started.
"""
from __future__ import annotations

import io
import posixpath
import re
import zipfile

from .taxonomy import parse_deliverables

# Signals that the project must be BUILT AND RUN to be seen at all — a
# dependency manifest or framework source that a browser cannot execute
# directly. Only these justify spinning up a (billable) sandbox.
#
# Deliberately narrow. A stray `helper.js` or `findings.json` beside a PDF
# report is not a web project, so generic `.js`/`.json`/`.css` are
# excluded here on purpose.
_DEPLOY_FILENAMES = {"package.json"}
_DEPLOY_EXTENSIONS = {".jsx", ".tsx", ".vue", ".svelte", ".astro"}
# Config files whose *stem* identifies a framework regardless of whether
# the harness wrote the .js/.ts/.mjs/.cjs variant.
_DEPLOY_STEMS = {"vite.config", "next.config", "nuxt.config", "svelte.config", "astro.config"}

# Document formats a web task's own deliverables list can still legitimately
# name (a report alongside the app) but that can never themselves be part
# of a running site — see partition_deliverables.
_NON_WEBSITE_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".ppt", ".pptx"}

# Plain HTML deliberately isn't a signal. A single .html file is just a
# file — the existing inline renderer already displays it, so it stays on
# the ordinary path with no project handling and no sandbox.

# Directory names that mean "this is the server half" — skipped when
# choosing what to actually run (see find_frontend_root).
_BACKEND_DIR_NAMES = {"backend", "server", "api", "services", "functions"}
# Directory names that mean "this is the browser half", preferred when a
# project ships both.
_FRONTEND_DIR_NAMES = {"frontend", "client", "web", "ui", "app", "site"}


def _parts(name: str) -> tuple[str, str, str]:
    base = posixpath.basename(name.strip().replace("\\", "/")).lower()
    stem, ext = posixpath.splitext(base)
    return base, stem, ext


def _is_deploy_file(name: str) -> bool:
    base, stem, ext = _parts(name)
    if not base:
        return False
    return base in _DEPLOY_FILENAMES or ext in _DEPLOY_EXTENSIONS or stem in _DEPLOY_STEMS


def _has_deploy_file_on_disk(workdir: str) -> bool:
    """Whether the agent's actual output already contains a deploy signal,
    independent of whatever the task's `expected_deliverables` text names.

    A task author's expected-deliverables list is often just the one or two
    headline files they cared about (e.g. `index.html`), even when the
    agent goes on to build a real React/Vite project around it. Scanning
    what was actually written catches that case; it deliberately walks the
    whole tree rather than trusting `before`/`after` diffs, since a
    baseline `package.json` the agent never touched is just as much a
    signal as one it wrote from scratch."""
    import os

    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if d.lower() not in _SCAN_IGNORED_DIRS]
        for name in files:
            if _is_deploy_file(name):
                return True
    return False


# Kept separate from _collect.py's WEB_IGNORED_DIRS (same values) rather
# than imported from it: webproject.py has no other dependency on the
# harness-collection module, and this scan runs before that module's own
# decision about which collector to use.
_SCAN_IGNORED_DIRS = {
    "node_modules", ".git", ".next", ".nuxt", ".svelte-kit", ".vercel",
    "dist", "build", "out", "coverage", "__pycache__", ".venv", "venv",
    ".cache", ".parcel-cache", ".turbo",
}


def is_web_project(expected_deliverables: str, workdir: str | None = None) -> bool:
    """Whether this task's output has to be BUILT AND RUN to be judged.

    The one question everything keys off: it decides both that the run's
    whole file tree gets collected and that the run is offered a live
    preview. Plain HTML is not included — a .html file is just a file, and
    the existing inline renderer already shows it, so it needs neither.

    Checked first against the task's own `expected_deliverables` text (the
    declared intent, available before a run ever happens). When `workdir`
    is also given — a harness passes it right after the agent finishes —
    and the declared text alone doesn't say "web project", the actual
    files on disk are checked too, so a task whose expected-deliverables
    list only names e.g. `index.html` still gets routed to
    `collect_web_project` (and its broad `.css`/`.js`/etc. allow-list)
    when the agent in fact built a real framework project around it.
    Without this fallback, such a run falls into `collect_deliverables`'s
    much narrower, name-matched filter, which silently drops files the
    surviving source still imports (e.g. `styles.css`) and the deployed
    preview fails to build."""
    if any(_is_deploy_file(name) for name in parse_deliverables(expected_deliverables)):
        return True
    if workdir:
        return _has_deploy_file_on_disk(workdir)
    return False


def _package_json_score(relpath: str, raw: bytes) -> int:
    """How much a given package.json looks like the FRONTEND's one.

    Higher wins. Deliberately content-based rather than path-based alone:
    a project may put its frontend at the repo root with the server in
    `api/`, or the other way around, and only the dependency list
    reliably says which is which."""
    import json

    try:
        pkg = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, AttributeError):
        return 0
    deps = {}
    for key in ("dependencies", "devDependencies"):
        value = pkg.get(key)
        if isinstance(value, dict):
            deps.update(value)
    names = {str(k).lower() for k in deps}

    score = 1
    if names & {"react", "vue", "svelte", "next", "nuxt", "@angular/core", "astro", "solid-js", "preact"}:
        score += 10
    if names & {"vite", "@vitejs/plugin-react", "webpack", "parcel", "next"}:
        score += 5
    # An explicit server framework at the TOP level of this package means
    # this is the backend's manifest, not the frontend's.
    if names & {"express", "fastify", "koa", "nest", "@nestjs/core", "hapi"} and not (
        names & {"react", "vue", "svelte", "next", "nuxt"}
    ):
        score -= 8

    parts = [p.lower() for p in posixpath.dirname(relpath).split("/") if p]
    if any(p in _FRONTEND_DIR_NAMES for p in parts):
        score += 4
    if any(p in _BACKEND_DIR_NAMES for p in parts):
        score -= 6
    # Shallower is more likely to be the primary app.
    score -= len(parts)
    return score


def find_frontend_root(files: dict[str, bytes]) -> str:
    """Pick the directory to actually deploy, as a '' or 'sub/dir' path.

    `files` maps relative POSIX paths to bytes. Prefers the best-scoring
    package.json's directory; falls back to the shallowest directory
    holding an index.html (a plain static site with no manifest); falls
    back to the project root."""
    best_dir, best_score = None, 0
    for relpath, raw in files.items():
        if posixpath.basename(relpath).lower() != "package.json":
            continue
        score = _package_json_score(relpath, raw)
        if score > best_score:
            best_dir, best_score = posixpath.dirname(relpath), score
    if best_dir is not None:
        return best_dir

    html_dirs = [
        posixpath.dirname(p)
        for p in files
        if posixpath.basename(p).lower() == "index.html"
        and not any(part.lower() in _BACKEND_DIR_NAMES for part in posixpath.dirname(p).split("/") if part)
    ]
    if html_dirs:
        return min(html_dirs, key=lambda d: (len([p for p in d.split("/") if p]), d))
    return ""


def partition_deliverables(
    deliverables: list[dict], package_json_bytes: dict[str, bytes], expected_deliverables: str
) -> tuple[set[int], set[int]]:
    """Split a run's deliverables into (website_ids, extra_ids) for judging.

    A web-project run's whole point is the running site, not any one
    source file in isolation — a judge scoring `App.jsx`, `App.css`, and
    `vite.config.js` as three unrelated 1-10s is scoring the same live
    preview three times over. `website_ids` is every deliverable that
    lives inside the deployed frontend root (see `find_frontend_root`,
    same rule sandbox_deploy.py uses to pick what actually gets served) —
    the judging UI collapses these into one score tied to the Preview tab
    instead of one row per file.

    `extra_ids` is deliverables OUTSIDE that root whose filename is one of
    the task's own `expected_deliverables` — a report or dataset a web
    task explicitly also asked for keeps its ordinary one-score-per-file
    treatment. A stray file outside the root that ISN'T on that list
    (leftover config, a `.gitignore` pulled from the sandbox) lands in
    neither set: still visible in the code tree, never required to be
    scored — it was never something the task asked for.

    The root itself doesn't always draw a useful line: the common case is
    a single app with no subdirectory at all, where `find_frontend_root`
    returns `""` — the whole flat tree gets deployed together (see
    sandbox_deploy.py's `_subtree`), so path containment alone can't tell
    a source file from a document that happened to ride along beside it.
    `_NON_WEBSITE_EXTENSIONS` covers that gap for the formats the rest of
    this arena's non-web tasks actually produce (xlsx/docx/pdf/csv/pptx,
    see README's "most tasks produce documents") — one of those inside
    the root still isn't part of the running site.

    Each item in `deliverables` needs `id`/`relpath`/`filename`.
    `package_json_bytes` maps relpath (or bare filename, matching however
    `deliverables` names its `package.json` entries) to that file's raw
    content — only `package.json` bytes matter here, see
    `_package_json_score`. Both empty for anything that isn't a web
    project at all, or has no deliverables yet — the caller's legacy
    per-file scoring path is exactly what an empty `website_ids` means."""
    if not deliverables or not is_web_project(expected_deliverables):
        return set(), set()

    def _path(d: dict) -> str:
        return d.get("relpath") or d["filename"]

    files_stub = {_path(d): package_json_bytes.get(_path(d), b"") for d in deliverables}
    root = find_frontend_root(files_stub)
    prefix = f"{root}/" if root else ""

    def _in_root(path: str) -> bool:
        return path == root or path.startswith(prefix)

    website_ids = {
        d["id"]
        for d in deliverables
        if _in_root(_path(d)) and posixpath.splitext(_path(d))[1].lower() not in _NON_WEBSITE_EXTENSIONS
    }

    expected_names = {name.strip().lower() for name in parse_deliverables(expected_deliverables)}
    extra_ids = {
        d["id"] for d in deliverables if d["id"] not in website_ids and (d.get("filename") or "").lower() in expected_names
    }
    return website_ids, extra_ids


def detect_framework(files: dict[str, bytes], root: str) -> tuple[str, int, str]:
    """Return (framework, port, start_command) for the chosen root.

    The start command runs inside the sandbox's /vercel/sandbox workdir.
    Every server binds 0.0.0.0 — the sandbox proxy can't reach a
    localhost-bound listener (see sandbox_deploy.py's notes)."""
    import json

    def _rel(name: str) -> str:
        return posixpath.join(root, name) if root else name

    names = {posixpath.relpath(p, root) if root else p for p in files if not root or p.startswith(f"{root}/")}
    lowered = {n.lower() for n in names}

    pkg_raw = files.get(_rel("package.json"))
    deps: set[str] = set()
    scripts: dict = {}
    if pkg_raw:
        try:
            pkg = json.loads(pkg_raw.decode("utf-8", errors="replace"))
            for key in ("dependencies", "devDependencies"):
                value = pkg.get(key)
                if isinstance(value, dict):
                    deps.update(str(k).lower() for k in value)
            if isinstance(pkg.get("scripts"), dict):
                scripts = pkg["scripts"]
        except ValueError:
            pass

    if "next" in deps or any(n.startswith("next.config") for n in lowered):
        return "next", 3000, "npx --yes next dev -H 0.0.0.0 -p 3000"
    if "vite" in deps or any(n.startswith("vite.config") for n in lowered):
        return "vite", 5173, "npx --yes vite --host 0.0.0.0 --port 5173"
    if pkg_raw and "dev" in scripts:
        # Unknown toolchain but it declares a dev script — trust it, and
        # rely on the port we publish matching its default only if it
        # happens to; otherwise the readiness poll fails and the caller
        # falls back to the zip rather than reporting a dead URL as live.
        return "node", 5173, "npm run dev"
    if pkg_raw and "start" in scripts:
        return "node", 5173, "npm start"
    # No manifest at all: plain static files, served rather than built.
    return "static", 5173, "npx --yes serve --listen 0.0.0.0:5173 ."


_VITE_CONFIG_NAMES = ("vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.cjs")
_ALLOWED_HOSTS_RE = re.compile(r"allowedHosts\s*:\s*(\[[^\]]*\]|\"[^\"]*\"|'[^']*'|[A-Za-z0-9_.]+)")
_SERVER_BLOCK_RE = re.compile(r"server\s*:\s*\{")
_DEFINE_CONFIG_RE = re.compile(r"defineConfig\s*\(\s*\{")


def patch_vite_allowed_hosts(files: dict[str, bytes], framework: str) -> dict[str, bytes]:
    """Force Vite's `server.allowedHosts` to the literal `true`.

    The sandbox serves the app from a freshly-random subdomain on every
    deploy, so anything narrower than `true` — Vite's default, or a host
    list the model pinned to something it guessed — answers every request
    with "Blocked request. This host is not allowed" and the preview is a
    blank page. This is applied over whatever config the harness actually
    wrote rather than trusting the prompt's instruction to have been
    followed, because a single wrong line here breaks the whole preview
    and the failure looks identical to a broken app.

    Returns a new mapping; non-Vite projects pass through untouched.
    """
    if framework != "vite":
        return files
    patched = dict(files)
    for name in _VITE_CONFIG_NAMES:
        raw = patched.get(name)
        if raw is None:
            continue
        source = raw.decode("utf-8", errors="replace")
        original = source
        if _ALLOWED_HOSTS_RE.search(source):
            source = _ALLOWED_HOSTS_RE.sub("allowedHosts: true", source, count=1)
        elif _SERVER_BLOCK_RE.search(source):
            source = _SERVER_BLOCK_RE.sub('server: { allowedHosts: true, host: "0.0.0.0",', source, count=1)
        elif _DEFINE_CONFIG_RE.search(source):
            source = _DEFINE_CONFIG_RE.sub(
                'defineConfig({ server: { allowedHosts: true, host: "0.0.0.0" },', source, count=1
            )
        if source != original:
            patched[name] = source.encode("utf-8")
    return patched


def build_zip(files: dict[str, bytes]) -> bytes:
    """Zip the whole project — backend files included. This is what the
    user downloads whenever a preview isn't available, so it deliberately
    carries everything the harness produced, not just the deployed
    frontend subset."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for relpath, content in sorted(files.items()):
            archive.writestr(relpath, content)
    return buffer.getvalue()


def safe_relpath(relpath: str) -> str | None:
    """Normalize a harness-produced path, or None if it tries to escape.

    Deliverable relpaths come from model output, so `../../etc/passwd` and
    absolute paths are both possible and are rejected rather than
    normalized into something surprising."""
    cleaned = (relpath or "").strip().replace("\\", "/").lstrip("/")
    if not cleaned:
        return None
    normalized = posixpath.normpath(cleaned)
    if normalized.startswith("../") or normalized == ".." or posixpath.isabs(normalized):
        return None
    return normalized
