"""Real Claude Code harness adapter.

Spawns the actual `claude` CLI headlessly, in the run's own ephemeral
workdir, and collects whatever deliverable files it writes there.

Important caveat, not hidden here: Claude Code only understands the
Anthropic Messages API shape. `ANTHROPIC_BASE_URL` works for Anthropic
itself or an Anthropic-compatible relay — it does NOT work with a raw
OpenAI-shaped third-party endpoint pointed at directly (e.g. DeepSeek's own
native API); the CLI itself would report a provider error in that case,
surfaced as a normal failed run rather than crashing. **Any model works
through it anyway** when the profile's `base_url` is OpenRouter
(openrouter.ai) — OpenRouter re-exposes third-party models under
Anthropic-compatible routing too. `_openrouter.py` auto-canonicalizes the
base URL and best-effort-prefixes a bare model name for this case, so a
profile just needs an OpenRouter key + `base_url` set to any
`openrouter.ai` URL — the model id can be typed either as OpenRouter's own
"vendor/model" slug or as a bare model name.

Also writes the bundled Internet Agent API schema into the workdir and
tells the agent about it — see `_internet.py`.

"""
from __future__ import annotations

import asyncio
import json
import os

from ._collect import (
    DeliverableLimitExceeded,
    collect_deliverables,
    collect_web_project,
    communicate_with_deliverable_timeout,
    snapshot,
)
from ._internet import write_schema
from ._openrouter import is_openrouter, normalize_model, ANTHROPIC_COMPATIBLE_BASE_URL
from ._prompt import build_prompt
from ._reference_files import write_reference_files
from .base import ProviderSettings, RunResult
from ..taxonomy import parse_deliverables
from ..webproject import is_web_project

TIMEOUT_SECONDS = float(os.environ.get("ARENA_HARNESS_TIMEOUT_SECONDS", "7200"))
CLAUDE_BIN = os.environ.get("ARENA_CLAUDE_BIN", "claude")

# Never let a literal secret value end up in a stored raw_log/error_message.
def _scrub(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def _last_result_event(stdout_text: str) -> dict | None:
    """`--output-format stream-json` emits one JSON object per line; the
    final `type: "result"` line carries the outcome — mirrors
    run_optima_claude_code.py's stream parsing."""
    last = None
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            last = event
    return last


def _live_text_for_event(event: dict) -> str | None:
    """Pull the human-readable part out of one stream-json event for the
    LIVE view, instead of forwarding the raw JSON object — a nested
    Anthropic-Messages-shaped `assistant`/`user` event reads as noise
    (ids, uuids, usage counts) with the actual text buried inside it.
    Returns None for an event with nothing worth showing live (the caller
    decides what to do with those, e.g. drop system chatter, keep an
    unrecognized shape as a raw fallback)."""
    etype = event.get("type")
    if etype == "assistant":
        parts = []
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                parts.append(block["text"])
            elif block.get("type") == "tool_use":
                parts.append(f"[tool_use: {block.get('name', 'tool')}]")
        return "\n".join(parts) if parts else None
    if etype == "user":
        # Tool results come back as a "user" turn in Claude Code's
        # stream-json — this is the CLI relaying the tool's own output
        # back to the model, not something the human operator typed.
        parts = []
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            result_content = block.get("content")
            if isinstance(result_content, str):
                text = result_content
            elif isinstance(result_content, list):
                text = "\n".join(
                    b.get("text", "") for b in result_content if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                text = ""
            if text:
                parts.append(f"[tool_result] {text[:500]}")
        return "\n".join(parts) if parts else None
    if etype == "result":
        return f"[done: {event.get('subtype', 'result')}]"
    return None


class ClaudeCodeAdapter:
    key = "claude-code"
    name = "Claude Code"
    tagline = "Git-native pair programmer, surgical diffs, zero fluff."

    async def run(self, task, workdir: str, provider: ProviderSettings) -> RunResult:
        if not provider.api_key:
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log="",
                error_message="No provider profile configured. Add an API key (or select a free-tier "
                "profile) in Setup before running Claude Code.",
            )

        internet_access = write_schema(workdir)
        attached_reference_filenames = write_reference_files(workdir, task)
        prompt = build_prompt(
            task,
            include_system_prompt=False,
            internet_access=internet_access,
            attached_reference_filenames=attached_reference_filenames,
        )
        system_prompt = (getattr(task, "system_prompt", "") or "").strip()

        via_openrouter = is_openrouter(provider.base_url)
        model = normalize_model(provider.model) if via_openrouter else provider.model

        command = [
            CLAUDE_BIN,
            "--print",
            "--verbose",
            "--bare",
            "--output-format", "stream-json",
            "--input-format", "text",
            "--no-session-persistence",
            "--dangerously-skip-permissions",
        ]
        if model:
            command += ["--model", model]
        if provider.reasoning_effort:
            command += ["--effort", provider.reasoning_effort]
        if system_prompt:
            command += ["--append-system-prompt", system_prompt]

        env = os.environ.copy()
        env["ANTHROPIC_API_KEY"] = provider.api_key
        if via_openrouter:
            env["ANTHROPIC_BASE_URL"] = ANTHROPIC_COMPATIBLE_BASE_URL
        elif provider.base_url:
            env["ANTHROPIC_BASE_URL"] = provider.base_url
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"

        before = snapshot(workdir)
        expected = parse_deliverables(getattr(task, "expected_deliverables", ""))

        # `--output-format stream-json` emits one JSON object per line, and
        # forwarding that raw object as "the live log" reads as noise (ids,
        # uuids, usage counts) with any actual text buried inside it — see
        # _live_text_for_event, which extracts just the readable part.
        # Filtering/extraction only affects the LIVE view; the final
        # raw_log below is still the true, unfiltered stdout tail.
        _live_buffer = ""

        def _live_output(chunk: str) -> None:
            nonlocal _live_buffer
            if not provider.live_log_callback:
                return
            _live_buffer += chunk
            lines = _live_buffer.split("\n")
            _live_buffer = lines.pop()  # last (possibly partial) line stays buffered
            for line in lines:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    provider.live_log_callback(_scrub(line, provider.api_key) + "\n")
                    continue
                if not isinstance(event, dict):
                    provider.live_log_callback(_scrub(line, provider.api_key) + "\n")
                    continue
                text = _live_text_for_event(event)
                if text is not None:
                    provider.live_log_callback(_scrub(text, provider.api_key) + "\n")
                elif event.get("type") != "system":
                    # An event type this function doesn't specifically
                    # recognize (future/unexpected shapes) — forward it raw
                    # rather than silently dropping something that might
                    # matter. Only plain "system" chatter (init, thinking
                    # tokens, ...) is dropped outright; it's the one type
                    # confirmed to carry nothing worth showing live.
                    provider.live_log_callback(_scrub(line, provider.api_key) + "\n")

        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=workdir,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log="",
                error_message=f"`{CLAUDE_BIN}` CLI not found on PATH.",
            )

        try:
            stdout_bytes, stderr_bytes, timed_out, completed_expected = await communicate_with_deliverable_timeout(
                proc, prompt, workdir, before, TIMEOUT_SECONDS, expected, on_output=_live_output,
            )
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        if timed_out:
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log="",
                error_message=f"Claude Code produced no new or updated deliverable for {TIMEOUT_SECONDS:.0f}s and was killed.",
            )

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = _scrub(stderr_bytes.decode("utf-8", errors="replace"), provider.api_key)

        # Trust the structured `result` event over the raw process exit code
        # when the two disagree: a nonzero exit alongside subtype "success"
        # means the turn genuinely completed and something unrelated (e.g. a
        # post-completion cleanup step) made the process exit noisily — that
        # is NOT a failure, and must never be reported as one (a subtype of
        # "success" is a real signal, not a placeholder "no detail" value).
        result_event = _last_result_event(stdout_text)
        if result_event is None and proc.returncode != 0 and not completed_expected:
            detail = stderr_text[-1500:].strip()
            # Some Claude CLI builds print a bare "success" token to stderr
            # even when the process exits nonzero without emitting the
            # structured result event. That is not an actionable failure
            # explanation, and displaying "failed: success" is misleading.
            if not detail or detail.lower() in {"success", "ok", "completed"}:
                detail = f"process exited with status {proc.returncode} before reporting a result"
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log=_scrub(stdout_text, provider.api_key)[-4000:],
                error_message=f"Claude Code failed: {detail}",
            )
        if not completed_expected and result_event is not None and result_event.get("subtype") not in (None, "success"):
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log=_scrub(stdout_text, provider.api_key)[-4000:],
                error_message=f"Claude Code did not finish cleanly: {result_event.get('subtype')}",
            )

        try:
            # A web project is collected as a whole tree rather than as the
            # named files alone — see _collect.collect_web_project.
            if is_web_project(getattr(task, "expected_deliverables", ""), workdir):
                deliverables = collect_web_project(workdir, before)
            else:
                deliverables = collect_deliverables(workdir, before, expected)
        except DeliverableLimitExceeded as exc:
            return RunResult(ok=False, deliverables=[], raw_log="", error_message=str(exc))

        if not deliverables:
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log=_scrub(stdout_text, provider.api_key)[-4000:],
                error_message="Claude Code finished but produced no deliverable files.",
            )

        return RunResult(
            ok=True,
            deliverables=deliverables,
            raw_log=_scrub(stdout_text, provider.api_key)[-4000:],
        )
