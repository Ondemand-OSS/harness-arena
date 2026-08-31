"""OpenCode CLI harness adapter."""
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
from ._openrouter import is_openrouter, normalize_model
from ._prompt import build_prompt
from ._reference_files import write_reference_files
from .base import ProviderSettings, RunResult
from ..taxonomy import parse_deliverables
from ..webproject import is_web_project

TIMEOUT_SECONDS = float(os.environ.get("ARENA_HARNESS_TIMEOUT_SECONDS", "7200"))
OPENCODE_BIN = os.environ.get("ARENA_OPENCODE_BIN", "opencode")

_PROVIDER_ID = "arena"
_OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"


def _scrub(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def _live_text_for_event(event: dict) -> str | None:
    """Pull the human-readable part out of one `--format json` event for
    the LIVE view — verified against a real run: a "text" event's actual
    assistant text lives at part.text; step_start/step_finish and anything
    else carry no prose (tokens/cost/session metadata only), so the caller
    falls back to a bare event-type breadcrumb for those."""
    if event.get("type") == "text":
        part = event.get("part")
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text:
                return text
    return None


class OpenCodeAdapter:
    key = "opencode"
    name = "opencode"
    tagline = "Terminal coding agent, any OpenAI-compatible endpoint."

    async def run(self, task, workdir: str, provider: ProviderSettings) -> RunResult:
        if not provider.api_key:
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log="",
                error_message="No provider profile configured. Add an API key (or select a free-tier "
                "profile) in Setup before running opencode.",
            )

        internet_access = write_schema(workdir)
        attached_reference_filenames = write_reference_files(workdir, task)
        prompt = build_prompt(
            task,
            include_system_prompt=True,
            internet_access=internet_access,
            attached_reference_filenames=attached_reference_filenames,
            skill_names=provider.workdir_skill_names,
        )

        via_openrouter = is_openrouter(provider.base_url)
        model = normalize_model(provider.model) if via_openrouter else provider.model
        base_url = provider.base_url or _OPENAI_DEFAULT_BASE_URL
        model_selector = f"{_PROVIDER_ID}/{model}"

        config_dir = os.path.join(workdir, ".opencode-config")
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, "opencode.json")
        config = {
            "$schema": "https://opencode.ai/config.json",
            "model": model_selector,
            "small_model": model_selector,
            "provider": {
                _PROVIDER_ID: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Arena endpoint",
                    "options": {
                        "baseURL": base_url,
                        "apiKey": provider.api_key,
                    },
                    "models": {model: {"name": model}},
                }
            },
            "permission": {"*": "allow"},
            "autoupdate": False,
            "share": "disabled",
        }
        with open(config_path, "w") as f:
            json.dump(config, f)

        command = [
            OPENCODE_BIN,
            "run",
            "--format", "json",
            "--model", model_selector,
            "--dir", workdir,
            "--auto",
            "--pure",
            # Verified via `opencode run --help`: writes to stderr, not
            # stdout, so this doesn't touch the stream-json stdout parsing
            # below — it only gives the live-log view (see runner.py's
            # persist_live_log) something to show besides silence between
            # `--format json` stdout events, which are otherwise sparse.
            "--print-logs",
            "--log-level", "INFO",
            "--",
            prompt,
        ]

        env = os.environ.copy()
        env["OPENCODE_CONFIG"] = config_path
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        env["OPENAI_API_KEY"] = provider.api_key

        before = snapshot(workdir)
        expected = parse_deliverables(getattr(task, "expected_deliverables", ""))

        # stdout carries `--format json` events (one per line); stderr
        # carries `--print-logs` diagnostic lines (plain key=value text,
        # not JSON) — both flow through this same callback. A JSON line
        # gets its actual assistant text extracted (see
        # _live_text_for_event); anything that isn't JSON, or is JSON with
        # no text (step_start/step_finish/...), is forwarded as-is so the
        # diagnostic lines and event-type breadcrumbs both stay visible.
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
                text = _live_text_for_event(event) if isinstance(event, dict) else None
                if text is not None:
                    provider.live_log_callback(_scrub(text, provider.api_key) + "\n")
                else:
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
                error_message=f"`{OPENCODE_BIN}` CLI not found on PATH.",
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
                error_message=f"opencode produced no new or updated deliverable for {TIMEOUT_SECONDS:.0f}s and was killed.",
            )

        stdout_text = _scrub(stdout_bytes.decode("utf-8", errors="replace"), provider.api_key)
        stderr_text = _scrub(stderr_bytes.decode("utf-8", errors="replace"), provider.api_key)

        if proc.returncode != 0 and not completed_expected:
            detail = stderr_text[-1500:].strip() or stdout_text[-1500:].strip()
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log=(stdout_text + "\n" + stderr_text)[-4000:],
                error_message=f"opencode exited with status {proc.returncode}: {detail or 'no output'}",
            )

        try:
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
                raw_log=(stdout_text + "\n" + stderr_text)[-4000:],
                error_message="opencode finished but produced no deliverable files.",
            )

        return RunResult(
            ok=True,
            deliverables=deliverables,
            raw_log=stdout_text[-4000:],
        )
