"""OnDemand harness adapter."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.parse
from pathlib import Path

import httpx

from ..taxonomy import parse_deliverables
from ..webproject import is_web_project
from ._prompt import build_prompt
from ._reference_files import reference_file_blobs
from .base import ProviderSettings, RunResult

BASE_URL = "https://api.on-demand.io"
CREATE_SESSION_URL = f"{BASE_URL}/chat/v1/sessions"
SUGGEST_PLUGINS_URL = f"{BASE_URL}/plugin/v1/suggest_plugins"
MEDIA_UPLOAD_URL = f"{BASE_URL}/media/v1/public/file/raw"
# Working directory inside a Vercel sandbox — where OnDemand's agent
# builds a web project, and so where its sources are read back from.
SANDBOX_ROOT = "/vercel/sandbox"

log = logging.getLogger(__name__)

DOCUMENT_AGENT_PLUGIN_ID = "plugin-1713954536"
TIMEOUT_SECONDS = float(os.environ.get("ARENA_HARNESS_TIMEOUT_SECONDS", "3600"))
# Fallback only — used when the admin hasn't set a reasoning_effort on the
# selected OnDemand model (routers/ondemand_models.py, resolved in
# runner.py). Kept at the cheapest tier by default rather than "max" so an
# admin who never configured a model doesn't unknowingly pay for the most
# expensive reasoning tier on every run.
REASONING_EFFORT = "low"
SUGGESTED_PLUGIN_LIMIT = 5

DEFAULT_AGENT_IDS = [
    "plugin-1775547203",
    "plugin-1722260873",
    "plugin-1712327325",
    "plugin-1743257072",
    "plugin-1713962163",
    "plugin-1741871229",
    "plugin-1776826082",
]

# Retry transient and throttling statuses with capped exponential backoff.
_MAX_SESSION_ATTEMPTS = 3
# Each retry recreates a whole session (see the loop below), not just the
# query — a 1s/2s backoff was barely giving a transient condition (a
# connection reset, a brief upstream blip) any time to actually clear
# before hammering it again with a fresh session. 90s (1.5 min) floor gives
# that a real chance; still doubles on top for the (currently unreachable,
# at only 2 waits total for 3 attempts) case of a third+ attempt.
_BACKOFF_MIN_SECONDS = 90
_BACKOFF_CAP_SECONDS = 180

# Separate, more generous budget for dropping inactive agents — free/no
# backoff, so it shouldn't eat into _MAX_SESSION_ATTEMPTS's real retries.
# Comfortably above DEFAULT_AGENT_IDS' own length (7) plus room for
# suggested plugins, so a real run of bad defaults can be fully exhausted
# rather than giving up with some still untried.
_MAX_INACTIVE_AGENT_REMOVALS = 12

# Resumes allowed after a dropped stream, on top of the initial request —
# so 1 means at most 2 requests per query in total. A drop that survives
# one resume is unlikely to be the transient blip this exists to paper
# over, and each further attempt re-reads the whole replayed answer.
_MAX_STREAM_RESUME_ATTEMPTS = 3

_TEXT_EXTENSIONS = {".md", ".txt", ".json", ".html"}


def _scrub(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def _append_attempt_diagnostics(answer: str, attempt_log: list[str], secret: str) -> str:
    if not attempt_log:
        return _scrub(answer, secret)
    diagnostics = _scrub("\n".join(attempt_log), secret)
    return f"{_scrub(answer, secret)}\n\n--- OnDemand session retry diagnostics ---\n{diagnostics}"


def _first_string(value) -> str | None:
    """Extract text from a nested response payload."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("data", "result", "response", "message", "content", "text", "answer"):
            if key in value:
                nested = _first_string(value[key])
                if nested is not None:
                    return nested
        return None
    if isinstance(value, list):
        parts = [s for s in (_first_string(v) for v in value) if s]
        return "\n".join(parts) if parts else None
    return None


def _deliverable_filename(task) -> str:
    expected = parse_deliverables(getattr(task, "expected_deliverables", ""))
    if len(expected) == 1 and Path(expected[0]).suffix.lower() in _TEXT_EXTENSIONS:
        return expected[0]
    return "response.md"


_URL_RE = re.compile(r"https?://\S+")
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")


def _normalize_stem(stem: str) -> str:
    """Alphanumeric characters only, lowercased. OnDemand's own generated
    blob name doesn't just append a version suffix to the expected
    filename — it can collapse separators entirely (observed:
    `site_comparison_v1.csv` came back as a blob named
    `sitecomparisonv1.csv`, underscores just gone). Comparing only on
    alphanumerics is what actually lines the two up; stripping just a
    trailing "_v1"/"-v1" (an earlier version of this) still fails the
    moment the SEPARATOR itself goes missing, not only the version digit."""
    return re.sub(r"[^a-z0-9]", "", stem.lower())


def _extract_urls(text: str) -> list[str]:
    # A trailing `)`, `.`, `,`, `]` etc. is markdown/prose punctuation, not
    # part of the URL — SAS query strings never end in those themselves.
    # `]` matters here specifically: a reference-style link or a stray
    # bracket right after a bare URL (no matching `[` swallowed into the
    # match) leaves one in the netloc, which urllib.parse.urlparse rejects
    # outright (see _url_basename_stem_ext).
    return [url.rstrip(").,;\"'>]") for url in _URL_RE.findall(text)]


def _url_basename_stem_ext(url: str) -> tuple[str, str]:
    """('', '') for a URL urllib can't parse, rather than raising.

    `urlparse` raises `ValueError("Invalid IPv6 URL")` for a netloc with an
    unbalanced `[`/`]` (it's checking for IPv6-literal syntax like
    `[::1]`) — reachable here despite the stripping in `_extract_urls`
    whenever the stray bracket sits mid-URL rather than trailing, or SSE
    event payloads hand a malformed URL straight to this function. One
    unparseable URL among several extracted from freeform model text must
    not take down the whole run — the caller just treats it as no match
    and keeps checking the rest."""
    try:
        name = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
    except ValueError:
        return "", ""
    stem, ext = os.path.splitext(name)
    return stem.lower(), ext.lower()


def _match_url_for_filename(text: str, urls: list[str], filename: str) -> str | None:
    """OnDemand names the blob after the file it's actually delivering, just
    with its own separators/version suffix mangled — so same extension +
    a normalized stem that lines up is enough, without requiring an exact
    filename match neither side fully controls (checked first, since it's
    the documented "Deliverables URLs" case). Falls back to a markdown
    link whose LABEL (not its URL) names the file — e.g. a "Documents:"
    listing elsewhere in the same answer — for whichever expected files
    the first pass didn't find a match for."""
    want_stem, want_ext = os.path.splitext(filename)
    want_norm = _normalize_stem(want_stem)
    want_ext = want_ext.lower()
    for url in urls:
        stem, ext = _url_basename_stem_ext(url)
        if ext != want_ext:
            continue
        norm_stem = _normalize_stem(stem)
        if norm_stem == want_norm or want_norm in norm_stem or norm_stem in want_norm:
            return url
    for label, url in _MD_LINK_RE.findall(text):
        if _normalize_stem(label) == want_norm:
            return url
    return None


async def _collect_url_deliverables(
    client: httpx.AsyncClient, answer: str, task, workdir: str, event_urls: list[str] | None = None
) -> list[str]:
    """Download whichever expected deliverable files OnDemand's answer
    actually linked to (see module docstring) straight into `workdir`,
    under their EXPECTED filename rather than OnDemand's versioned blob
    name. Best-effort: a download failure for one file just skips it,
    it doesn't fail the whole run."""
    expected = parse_deliverables(getattr(task, "expected_deliverables", ""))
    # Plugin-reported URLs come FIRST: they are the execution agent's own
    # statement of what it generated, whereas anything scraped out of the
    # answer text depends on the model having chosen to paste its links.
    # De-duplicated, order preserved, so a file named in both is fetched once.
    urls = list(dict.fromkeys([*(event_urls or []), *_extract_urls(answer)]))
    if not expected or not urls:
        return []
    downloaded = []
    for filename in expected:
        url = _match_url_for_filename(answer, urls, filename)
        if not url:
            continue
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError:
            continue
        with open(os.path.join(workdir, filename), "wb") as f:
            f.write(resp.content)
        downloaded.append(filename)
    return downloaded


async def _upload_reference_files(
    client: httpx.AsyncClient, task, session_id: str, api_key: str
) -> tuple[list[str], list[tuple[str, str]]]:
    """Best-effort: uploads each attached reference file to the session
    (see module docstring). Returns `(uploaded, failed)`:
    `uploaded` is the filenames that actually made it through — empty if
    the task has none attached, or if every upload failed. `failed` is
    `(filename, text_content)` for every attached file whose upload call
    itself errored (rate limit, transient failure) — these still have
    real bytes sitting right here in memory, so the caller inlines their
    actual content into the prompt directly (see `_prompt.py`'s
    `inlined_reference_files`) rather than silently degrading to the
    dataset row's own vague text-only mention. Reference files are
    `.md`-only (see routers/tasks.py's upload validation), so decoding as
    UTF-8 text is always safe.

    Deliberately sends only the `apikey` header, not the JSON
    `Content-Type` the other two calls use — httpx needs to set its own
    multipart boundary Content-Type for a `files=` request, which an
    explicit `application/json` header would override and break. `data` is
    a plain dict (a single `plugins` value, not a repeated field) rather
    than a list of tuples: httpx's async multipart path was found to build
    a sync (not awaitable) request body when `data` is a list, breaking
    the send outright — see the test in this change's history for the
    reproduction; a dict avoids that path entirely."""
    uploaded = []
    failed = []
    for blob in reference_file_blobs(task):
        filename = os.path.basename(blob.get("filename") or "")
        content = blob.get("content")
        if not filename or not content:
            continue
        files = {"file": (filename, content)}
        data = {
            "sessionId": session_id,
            "name": filename,
            "sizeBytes": str(len(content)),
            "responseMode": "sync",
            "plugins": DOCUMENT_AGENT_PLUGIN_ID,
        }
        try:
            resp = await client.post(MEDIA_UPLOAD_URL, data=data, files=files, headers={"apikey": api_key})
            resp.raise_for_status()
        except httpx.HTTPError:
            failed.append((filename, content.decode("utf-8", errors="replace")))
            continue
        uploaded.append(filename)
    return uploaded, failed


def _request_error_detail(exc: httpx.RequestError) -> str:
    """Return transport error details, including the chained cause when present."""
    own = str(exc).strip()
    cause = exc.__cause__
    cause_str = str(cause).strip() if cause else ""
    if own and cause_str:
        return f"{own} (caused by {type(cause).__name__}: {cause_str})"
    if cause_str:
        return f"{type(cause).__name__}: {cause_str}"
    if own:
        return own
    return "no additional detail"


def _is_invalid_api_key_error(error: str) -> bool:
    """Whether a session-create failure is OnDemand rejecting the API key
    itself, e.g. `HTTP 401: {"errorCode":"unauthenticated","message":"Invalid
    API Key"}` — as opposed to a transient/unrelated failure. Retrying a bad
    key across the usual attempt loop just repeats the same 401 three times
    for no benefit, and the raw JSON is not something a user should have to
    parse to understand what's wrong."""
    if "401" not in error:
        return False
    lowered = error.lower()
    return "unauthenticated" in lowered or "invalid api key" in lowered


def _inactive_agent_ids(error: str) -> set[str]:
    """Extract IDs from OnDemand's ``inactiveAgentIds`` error field.

    OnDemand may return this JSON as a failed HTTP response or inside an SSE
    ``[ERROR]`` event. The HTTP path adds a status prefix, so the complete
    error string is not necessarily valid JSON.
    """
    match = re.search(r'"inactiveAgentIds"\s*:\s*(\[[^\]]*\])', error)
    if not match:
        return set()
    try:
        values = json.loads(match.group(1))
    except ValueError:
        return set()
    return {value for value in values if isinstance(value, str) and value}


async def _post_once(
    client: httpx.AsyncClient, url: str, body: dict, headers: dict, secret: str
) -> tuple[dict | None, str | None]:
    try:
        resp = await client.post(url, json=body, headers=headers)
    except httpx.RequestError as exc:
        detail = _request_error_detail(exc)
        return None, f"request failed ({type(exc).__name__}): {detail}"
    if not 200 <= resp.status_code < 300:
        return None, f"HTTP {resp.status_code}: {_scrub(resp.text, secret)}"
    try:
        return resp.json(), None
    except ValueError:
        return None, "response was not valid JSON"


async def _iter_sse_data(lines) -> "AsyncIterator[str]":
    """Groups raw SSE lines into event `data` payloads.

    Per the SSE spec, an event's data can span several consecutive `data:`
    lines (joined with `\\n`) and ends at the first blank line; a leading
    `:` line is a comment and a bare `event:`/`id:` line is metadata this
    adapter doesn't need. This is written generically rather than assuming
    "one line == one event", even though every live payload seen so far was
    single-line, because a longer fulfillment chunk wrapping onto a second
    `data:` line is legal SSE and would silently drop half the chunk if we
    only read the first line.
    """
    buf: list[str] = []
    async for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if buf:
                yield "\n".join(buf)
                buf = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            buf.append(line[5:].lstrip(" ") if line[5:6] == " " else line[5:])
    if buf:
        yield "\n".join(buf)


def _harvest_preview(event: dict, state: dict) -> None:
    """Record the sandbox OnDemand deployed for a web task, if this event
    announces one.

    For web-development tasks OnDemand's agent builds the project and
    deploys it to an ephemeral Vercel sandbox itself, then reports it.
    Captured live (session 6a8e095e...), it arrives on three separate
    events, any of which may be the one that survives:

      ondemand_agent.preview_ready   -> data.url
      ondemand_agent.sandbox_created -> data.sandbox.{id,url,port}
      ondemand_agent.completed       -> data.previewUrl, data.sandbox{...}

    Reusing that URL means a web run has a live preview immediately and at
    no cost to us. The sandbox id matters just as much as the URL: the
    sandbox lives in the same Vercel team as our own token, so it is the
    only reliable way to recover the SOURCE for a redeploy after it
    expires — that same run's `generatedFileUrls` held only screenshots
    and its `file_url` was empty, with the code pasted into the answer as
    markdown rather than packaged."""
    data = event.get("data")
    if not isinstance(data, dict):
        return
    if event.get("eventType") == "ondemand_agent.preview_ready":
        url = data.get("url")
        if isinstance(url, str) and ".vercel.run" in url:
            state["preview_url"] = url
    for key in ("previewUrl", "preview_url"):
        url = data.get(key)
        if isinstance(url, str) and ".vercel.run" in url:
            state["preview_url"] = url
    box = data.get("sandbox")
    if isinstance(box, dict):
        if isinstance(box.get("id"), str):
            state["sandbox_id"] = box["id"]
        if isinstance(box.get("url"), str) and ".vercel.run" in box["url"]:
            state.setdefault("preview_url", box["url"])
        if isinstance(box.get("port"), int):
            state["port"] = box["port"]


# Generated dependency and build directories excluded from source capture.
_SANDBOX_SKIP_DIRS = ("node_modules", ".git", "dist", "build", ".next", ".vercel", ".cache")
_SANDBOX_MAX_FILES = 300
_SANDBOX_MAX_BYTES = 5 * 1024 * 1024


async def _pull_sandbox_sources(sandbox_id: str, workdir: str) -> list[str]:
    """Copy the project OnDemand built out of its own sandbox into
    `workdir`, so the run owns its source.

    Without this a web run is a preview with nothing behind it: the moment
    OnDemand's sandbox expires there is no code left to redeploy or judge,
    because OnDemand packages screenshots rather than sources. The sandbox
    is reachable because it is created inside the same Vercel team our
    token belongs to (verified against a live one).

    Best-effort throughout — any failure just means the run keeps whatever
    deliverables it already had."""
    try:
        from vercel.sandbox import get_sandbox
    except ImportError:
        return []
    try:
        sandbox = await get_sandbox(name=sandbox_id)
        prune = " ".join(f"-not -path '*/{d}/*'" for d in _SANDBOX_SKIP_DIRS)
        listing = await sandbox.run_process(
            "bash", ["-lc", f"find {SANDBOX_ROOT} -type f {prune} | head -{_SANDBOX_MAX_FILES}"],
            capture_output=True,
        )
        paths = [p.strip() for p in (listing.stdout or "").splitlines() if p.strip()]
        written: list[str] = []
        for path in paths:
            rel = os.path.relpath(path, SANDBOX_ROOT)
            if rel.startswith(".."):
                continue
            try:
                blob = await sandbox.fs.read_bytes(path)
            except Exception:  # noqa: BLE001 - one unreadable file is not fatal
                continue
            if len(blob) > _SANDBOX_MAX_BYTES:
                continue
            target = os.path.join(workdir, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(blob)
            written.append(rel)
        return written
    except Exception:  # noqa: BLE001 - previews must never fail a run
        log.warning("could not pull sources from OnDemand sandbox %s", sandbox_id, exc_info=True)
        return []


def _harvest_event_urls(value, sink: list[str], depth: int = 0) -> None:
    """Collect every http(s) URL anywhere inside one SSE event payload.

    OnDemand reports the files its execution agent generated as STRUCTURED
    event fields, not as links in the answer text. Reading only
    `eventType == "fulfillment"` threw those away, so a run whose prose
    happened not to repeat the links stored the model's summary as
    `response.md` and silently discarded every real file it had produced
    (observed on session 6a8df757...: six expected deliverables, one
    useless markdown file kept).

    Captured live rather than assumed — a probe run emitted 18 distinct
    event types, and blob URLs arrived on three of them in two different
    shapes:

      ondemand_agent.completed -> data.outputBlobUrl (str)
                                  data.generatedFileUrls (list[str])
      executePrompt plugin     -> file_url (str)
                                  intermediate_files (list[str])

    Neither key set is a contract, and the plugin's report has also been
    seen as human-readable text with its JSON embedded partway through
    ("Plugin Name: executePrompt: {...}"). So this walks the entire
    payload at any depth and regexes any string that mentions http,
    instead of reading named keys that differ per producer.

    Over-collecting is safe: `_match_url_for_filename` only accepts a URL
    whose extension AND normalized stem line up with an expected
    deliverable, so citation links from a web-search step cannot
    masquerade as one."""
    if depth > 6:
        return
    if isinstance(value, str):
        if value.startswith("http://") or value.startswith("https://"):
            sink.append(value.rstrip(").,;\"'>"))
        elif "http" in value:
            # The plugin's report is often a human-readable line with its
            # JSON payload embedded partway through
            # ("Plugin Name: executePrompt: {...}"), so this cannot assume
            # the string parses as JSON, or even that it starts with it —
            # regex over the raw text catches every one of those shapes.
            sink.extend(_extract_urls(value))
        return
    if isinstance(value, dict):
        for nested in value.values():
            _harvest_event_urls(nested, sink, depth + 1)
        return
    if isinstance(value, list):
        for nested in value[:200]:
            _harvest_event_urls(nested, sink, depth + 1)


def _merge_streamed(previous: str, resumed: str) -> str:
    """Join the text from before a dropped stream with what the resumed
    stream sent, without duplicating the overlap.

    Confirmed against the live API: a resumed query does NOT pick up
    emitting deltas from where the connection died. Its first fulfillment
    event carries the ENTIRE answer produced so far as one catch-up chunk
    (measured: 1270 chars replayed against 20 chars already received),
    and only then continues delta-by-delta. Concatenating naively
    therefore yields "Here are the numbersHere are the numbers 1 through
    30..." — a visibly corrupted deliverable, which is exactly what this
    exists to prevent.

    Handles the general case rather than only the observed one: a full
    replay collapses to `resumed`, a partial overlap is trimmed, and
    genuinely disjoint text still concatenates."""
    if not previous:
        return resumed
    if not resumed:
        return previous
    if resumed.startswith(previous):
        return resumed
    for size in range(min(len(previous), len(resumed)), 0, -1):
        if previous.endswith(resumed[:size]):
            return previous + resumed[size:]
    return previous + resumed


async def _stream_query(
    client: httpx.AsyncClient,
    url: str,
    body: dict,
    headers: dict,
    secret: str,
    log_callback=None,
) -> tuple[str | None, list[str], dict, str | None]:
    """Runs a `responseMode: "stream"` query, returning
    (answer, file_urls, preview, error).

    Reconstructing `answer` this way is the whole point of switching off
    `sync`: a sync query holds one socket open with literally zero bytes on
    the wire until the model is completely done, which on a slow task can
    be many minutes — exactly the shape of connection that an idle-timing-out
    proxy or load balancer kills, which is what the retry/backoff loop
    around this call exists to paper over (see the module docstring above
    _post_once). A stream query keeps bytes flowing the whole time
    (heartbeat/thinking/status events between fulfillment chunks), so
    there's nothing for an idle timeout to reset. `TIMEOUT_SECONDS` still
    bounds each individual read, but a read is now "time to the next SSE
    line," not "time to the entire answer."

    Confirmed against a live captured event: `eventType: "fulfillment"`
    events carry an `answer` field that is a DELTA (a chunk to append), not
    the full text so far — a captured chunk began mid-word
    ("lysis-market-outlook.docx", the tail of a longer filename split across
    a chunk boundary), which only makes sense if chunks are meant to be
    concatenated, not read as a running total. The
    stream ends with a literal `data: [DONE]` line, or `data: [ERROR]<json>`
    on failure — both are OnDemand's own sentinel strings, not JSON, so they
    have to be checked before attempting to parse the line as JSON.

    A connection dropping mid-stream (see _MAX_STREAM_RESUME_ATTEMPTS)
    doesn't discard what was already streamed: every event carries a
    `messageId` and an `eventIndex` (live capture: `{"sessionId": "...",
    "messageId": "68...", "eventIndex": 17, "eventType": "fulfillment",
    "answer": "...", ...}`), so re-POSTing to this same `url` with those
    two added to the body resumes the same in-flight message instead of
    starting a whole new query from scratch.
    """
    headers = {**headers, "Accept": "text/event-stream"}
    # Text merged from attempts that already finished. Kept separate from
    # the current attempt's buffer because a resumed stream replays what
    # we already have (see _merge_streamed).
    answer_so_far = ""
    # Files the execution plugin reported, harvested from event payloads
    # rather than from the answer text — see _harvest_event_urls.
    event_urls: list[str] = []
    # Sandbox OnDemand deployed for this task, if any (see _harvest_preview).
    preview: dict = {}
    saw_any_event = False
    message_id: str | None = None
    last_event_index: int | None = None
    request_body = body

    for resume_attempt in range(_MAX_STREAM_RESUME_ATTEMPTS + 1):
        attempt_parts: list[str] = []
        stream_error: str | None = None
        connection_error: str | None = None
        try:
            async with client.stream("POST", url, json=request_body, headers=headers) as resp:
                if not 200 <= resp.status_code < 300:
                    body_text = await resp.aread()
                    return None, event_urls, preview, f"HTTP {resp.status_code}: {_scrub(body_text.decode('utf-8', errors='replace'), secret)}"
                async for data in _iter_sse_data(resp.aiter_lines()):
                    if not data:
                        continue
                    if data == "[DONE]":
                        break
                    if data.startswith("[ERROR]"):
                        payload = data[len("[ERROR]"):].strip()
                        try:
                            parsed = json.loads(payload)
                            # Keep the structured payload when it carries
                            # inactiveAgentIds; the caller needs those IDs to
                            # remove them before the immediate retry.
                            detail = _first_string(parsed) or payload
                            stream_error = _scrub(payload if _inactive_agent_ids(payload) else detail, secret)
                        except ValueError:
                            stream_error = _scrub(payload, secret)
                        break
                    try:
                        event = json.loads(data)
                    except ValueError:
                        # Not one of OnDemand's own sentinels and not JSON either —
                        # a keepalive or a format this adapter doesn't know about.
                        # Ignoring it (rather than failing the whole run) is the
                        # same defensiveness _first_string already applies to an
                        # unrecognized JSON shape.
                        continue
                    saw_any_event = True
                    _harvest_event_urls(event, event_urls)
                    if isinstance(event, dict):
                        _harvest_preview(event, preview)
                        message_id = event.get("messageId") or message_id
                        if isinstance(event.get("eventIndex"), int):
                            last_event_index = event["eventIndex"]
                        if event.get("eventType") == "fulfillment":
                            chunk = event.get("answer")
                            if isinstance(chunk, str):
                                attempt_parts.append(chunk)
                                # The admin monitor must never receive the
                                # key carried in request headers. Scrub before
                                # handing the incremental text to the runner,
                                # rather than relying on its eventual final
                                # result to be scrubbed.
                                if log_callback:
                                    log_callback(_scrub(chunk, secret))
        except httpx.RequestError as exc:
            connection_error = f"request failed ({type(exc).__name__}): {_request_error_detail(exc)}"

        # Fold this attempt in whether it completed or died mid-stream —
        # a partial attempt's text is exactly what the next resume has to
        # be de-duplicated against.
        answer_so_far = _merge_streamed(answer_so_far, "".join(attempt_parts))

        if stream_error:
            return None, event_urls, preview, stream_error
        if connection_error is None:
            if not answer_so_far and not saw_any_event:
                return None, event_urls, preview, "stream produced no events"
            return answer_so_far, event_urls, preview, None

        # Connection dropped mid-stream. Resume from the last event we
        # actually saw, if we have one to resume from and haven't used up
        # the resume budget yet — otherwise this is a hard failure exactly
        # like before, and the caller's session-retry loop takes over.
        if message_id is None or resume_attempt >= _MAX_STREAM_RESUME_ATTEMPTS:
            return None, event_urls, preview, connection_error
        request_body = {**body, "messageId": message_id, "lastEventIndex": last_event_index}

    return None, event_urls, preview, "stream produced no events"  # unreachable — loop always returns above


def _extract_session_id(response_json: dict) -> str | None:
    data = response_json.get("data") if isinstance(response_json, dict) else None
    if isinstance(data, dict):
        for key in ("id", "sessionId", "sessionID", "session_id"):
            if data.get(key):
                return str(data[key])
    return None


async def _suggest_plugin_ids(client: httpx.AsyncClient, query: str, headers: dict, enabled: bool) -> list[str]:
    """`POST /plugin/v1/suggest_plugins` — task-specific plugins on top of
    the always-on DEFAULT_AGENT_IDS crew (e.g. a task that needs live web
    data gets a web-search plugin suggested for IT specifically, rather than
    every task permanently carrying one). Best-effort and non-fatal: a
    suggestion failure just means the task runs with only the defaults,
    same as before this existed — it never blocks or fails the run.

    `enabled` is the admin's routers/ondemand_models.py toggle, off by
    default — see that module for why."""
    if not enabled or not query:
        return []
    try:
        resp = await client.post(
            SUGGEST_PLUGINS_URL,
            json={"query": query, "limit": SUGGESTED_PLUGIN_LIMIT},
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, list):
        return []
    ids = []
    for plugin in plugins:
        if not isinstance(plugin, dict):
            continue
        plugin_id = plugin.get("pluginId") or plugin.get("id")
        if plugin_id:
            ids.append(str(plugin_id))
    return ids


class OnDemandAdapter:
    key = "ondemand"
    name = "OnDemand"
    tagline = "Multi-mode agent crew with per-task personas."
    enabled = True

    async def run(self, task, workdir: str, provider: ProviderSettings) -> RunResult:
        if not provider.ondemand_api_key:
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log="",
                error_message="No OnDemand API key set. Add your OnDemand API key in Setup before running OnDemand.",
            )
        if not provider.ondemand_endpoint_id:
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log="",
                error_message="No OnDemand model selected. Pick one in the model picker before running OnDemand.",
            )

        headers = {"apikey": provider.ondemand_api_key, "Content-Type": "application/json"}
        secret = provider.ondemand_api_key
        session_id = ""
        answer: str | None = None
        # Initialized here, not only inside the attempt loop: a run that
        # never reaches the query (session creation fails on every attempt)
        # still reads this on the way out.
        event_urls: list[str] = []
        preview_info: dict = {}
        query_error = ""
        attempt_log: list[str] = []
        inactive_agent_ids: set[str] = set()
        timeout = httpx.Timeout(
            TIMEOUT_SECONDS,
            connect=30.0,
            read=TIMEOUT_SECONDS,
            write=TIMEOUT_SECONDS,
            pool=30.0,
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            # Not re-fetched inside the retry loop below — one suggestion
            # call per run, whether or not that first attempt succeeds.
            suggested_ids = await _suggest_plugin_ids(
                client, getattr(task, "prompt", ""), headers, provider.ondemand_suggest_plugins_enabled
            )
            real_attempt = 0
            removal_round = 0
            # Agent ids we've stopped sending — see the inactive-agent
            # handling below for why this can't be keyed by the id OnDemand
            # actually reports.
            dropped_agent_ids: set[str] = set()
            while True:
                agent_ids = [
                    agent_id
                    for agent_id in dict.fromkeys([*DEFAULT_AGENT_IDS, *suggested_ids])
                    if agent_id not in dropped_agent_ids
                ]
                if not agent_ids:
                    query_error = "OnDemand marked every configured agent as inactive. Update the configured agent list."
                    break
                create_body = {
                    "agentIds": agent_ids,
                    "externalUserId": f"harness-arena-{getattr(task, 'id_aa', 'task')}",
                }
                create_json, create_error = await _post_once(client, CREATE_SESSION_URL, create_body, headers, secret)
                if create_json is None:
                    query_error = f"session creation failed: {create_error}"
                    if _is_invalid_api_key_error(create_error or ""):
                        # A bad key fails the exact same way on every retry —
                        # stop immediately instead of burning the attempt
                        # loop and its backoff on three identical 401s.
                        break
                else:
                    session_id = _extract_session_id(create_json) or ""
                    if not session_id:
                        query_error = "session creation didn't return a session id"
                    else:
                        if provider.ondemand_session_callback:
                            provider.ondemand_session_callback(session_id)
                        attached_reference_filenames, failed_reference_files = await _upload_reference_files(
                            client, task, session_id, secret
                        )
                        if attached_reference_filenames:
                            agent_ids = [
                                agent_id
                                for agent_id in dict.fromkeys([*agent_ids, DOCUMENT_AGENT_PLUGIN_ID])
                                if agent_id not in dropped_agent_ids
                            ]
                        query_body = {
                            "endpointId": provider.ondemand_endpoint_id,
                            "query": build_prompt(
                                task,
                                include_system_prompt=False,
                                attached_reference_filenames=attached_reference_filenames,
                                attached_reference_uploaded=True,
                                inlined_reference_files=failed_reference_files,
                                deliverable_urls=True,
                            ),
                            "agentIds": agent_ids,
                            "responseMode": "stream",
                            "reasoningEffort": provider.ondemand_reasoning_effort or REASONING_EFFORT,
                        }
                        answer, event_urls, preview_info, query_error = await _stream_query(
                            client,
                            f"{BASE_URL}/chat/v1/sessions/{session_id}/query",
                            query_body,
                            headers,
                            secret,
                            provider.ondemand_log_callback,
                        )
                        if answer is not None:
                            break
                rejected_agent_ids = _inactive_agent_ids(query_error or create_error or "")
                if rejected_agent_ids:
                    # OnDemand's inactiveAgentIds names its own internal
                    # agent-XXXX ids, never the plugin-XXXX ids we actually
                    # sent (see DEFAULT_AGENT_IDS) — there is no literal id
                    # here to exclude, so tracking "which agent-XXXX we've
                    # already seen" can't drive removal. Instead: drop the
                    # next not-yet-tried id we're actually sending and
                    # retry. Free (no backoff) and keeps going every time
                    # this error shows up, agent by agent, until the crew
                    # that's left succeeds or nothing is left to drop.
                    remaining = [a for a in agent_ids if a not in dropped_agent_ids]
                    if not remaining:
                        query_error = (
                            f"OnDemand keeps rejecting agents as inactive and every configured agent has "
                            f"already been dropped: {query_error}"
                        )
                        break
                    candidate = remaining[0]
                    dropped_agent_ids.add(candidate)
                    removal_round += 1
                    attempt_log.append(
                        f"OnDemand reported inactive agent(s) {', '.join(sorted(rejected_agent_ids))}"
                        f" (an internal id we can't map back to our own) — dropped {candidate} instead"
                        f" ({removal_round}/{_MAX_INACTIVE_AGENT_REMOVALS}); retrying."
                    )
                    if removal_round >= _MAX_INACTIVE_AGENT_REMOVALS:
                        break
                    continue
                attempt_log.append(
                    f"Attempt {real_attempt + 1}/{_MAX_SESSION_ATTEMPTS}"
                    f" (session {session_id or 'not created'}): {query_error or 'unknown failure'}"
                )
                real_attempt += 1
                if real_attempt >= _MAX_SESSION_ATTEMPTS:
                    break
                await asyncio.sleep(min(_BACKOFF_MIN_SECONDS * (2 ** (real_attempt - 1)), _BACKOFF_CAP_SECONDS))

        if answer is None:
            # The raw `query_error` (HTTP status + OnDemand's JSON body) is
            # kept in raw_log either way — this only swaps what a user sees
            # as the headline reason, for the one failure mode they can
            # actually act on themselves.
            if _is_invalid_api_key_error(query_error):
                friendly_error = "OnDemand rejected this API key as invalid. Check your OnDemand API key in Setup."
            else:
                friendly_error = f"OnDemand request failed: {query_error}"
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log="\n".join(attempt_log) or query_error,
                error_message=friendly_error,
                ondemand_session_id=session_id,
            )

        if not answer.strip():
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log=_scrub("\n".join(attempt_log), secret),
                error_message="OnDemand returned no usable answer text.",
                ondemand_session_id=session_id,
            )

        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as dl_client:
            downloaded = await _collect_url_deliverables(dl_client, answer, task, workdir, event_urls)

        # A web task that OnDemand deployed itself: copy the project out of
        # its sandbox so this run owns the source. OnDemand packages
        # screenshots rather than code, so without this the run is a
        # preview URL with nothing behind it — unjudgeable as soon as that
        # sandbox expires, and impossible to redeploy.
        pulled: list[str] = []
        if preview_info.get("sandbox_id") and is_web_project(getattr(task, "expected_deliverables", "")):
            pulled = await _pull_sandbox_sources(preview_info["sandbox_id"], workdir)
            if pulled:
                log.info("pulled %d source files from OnDemand sandbox %s", len(pulled), preview_info["sandbox_id"])
                downloaded = list(dict.fromkeys([*downloaded, *pulled]))

        if downloaded:
            deliverables = downloaded
        else:
            # Nothing URL-shaped in the answer at all — the old, plain-text
            # behavior: the answer itself IS the one deliverable.
            filename = _deliverable_filename(task)
            with open(os.path.join(workdir, filename), "w", encoding="utf-8") as f:
                f.write(answer.strip() + "\n")
            deliverables = [filename]

        return RunResult(
            ok=True,
            deliverables=deliverables,
            raw_log=_append_attempt_diagnostics(answer, attempt_log, secret),
            ondemand_session_id=session_id,
            preview_url=preview_info.get("preview_url", ""),
            preview_sandbox_id=preview_info.get("sandbox_id", ""),
        )
