"""Codex CLI harness adapter."""
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
from ._openrouter import is_openrouter, normalize_model, OPENAI_COMPATIBLE_BASE_URL
from ._prompt import build_prompt
from ._reference_files import write_reference_files
from .base import ProviderSettings, RunResult
from ..taxonomy import parse_deliverables
from ..webproject import is_web_project

TIMEOUT_SECONDS = float(os.environ.get("ARENA_HARNESS_TIMEOUT_SECONDS", "3600"))
CODEX_BIN = os.environ.get("ARENA_CODEX_BIN", "codex")

# OpenAI's own API doesn't need a custom `model_providers` override — Codex
# already defaults to it and just needs OPENAI_API_KEY in the environment.
_OPENAI_DEFAULT_HOSTS = {"api.openai.com"}


def _scrub(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def _is_openai_default(base_url: str) -> bool:
    base_url = (base_url or "").strip().lower()
    if not base_url:
        return True
    return any(host in base_url for host in _OPENAI_DEFAULT_HOSTS)


def _parse_events(stdout_text: str) -> tuple[bool, str | None]:
    """`codex exec --json` emits one JSON object per line. Returns
    (saw_a_completed_turn, failure_reason) — mirrors
    optima_codex_adapter.py's item/turn event handling."""
    saw_completed_turn = False
    failure: str | None = None
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        event_type = event.get("type") or event.get("item", {}).get("type")
        if event_type in ("turn.completed", "turn_completed"):
            saw_completed_turn = True
        elif event_type in ("turn.failed", "turn_failed", "error"):
            failure = str(event.get("error") or event.get("message") or event_type)
    return saw_completed_turn, failure


def _image_input_unsupported(stdout_text: str) -> bool:
    """Whether the provider rejected Codex's optional image-viewing input."""
    return "support image input" in stdout_text.lower()


class CodexAdapter:
    key = "codex"
    name = "Codex CLI"
    tagline = "Terminal harness, internet access enabled."

    async def run(self, task, workdir: str, provider: ProviderSettings) -> RunResult:
        if not provider.api_key:
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log="",
                error_message="No provider profile configured. Add an API key (or select a free-tier "
                "profile) in Setup before running Codex.",
            )

        internet_access = write_schema(workdir)
        attached_reference_filenames = write_reference_files(workdir, task)
        prompt = build_prompt(
            task,
            include_system_prompt=True,
            internet_access=internet_access,
            attached_reference_filenames=attached_reference_filenames,
        )

        via_openrouter = is_openrouter(provider.base_url)
        model = normalize_model(provider.model) if via_openrouter else provider.model

        command = [
            CODEX_BIN,
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd", workdir,
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
        ]
        if model:
            command += ["--model", model]
        if provider.reasoning_effort:
            command += ["--config", f'model_reasoning_effort="{provider.reasoning_effort}"']

        env = os.environ.copy()
        if _is_openai_default(provider.base_url):
            env["OPENAI_API_KEY"] = provider.api_key
        elif via_openrouter:
            command += [
                "--config", 'model_provider="arena"',
                "--config",
                'model_providers.arena={ name = "Arena", base_url = "%s", env_key = "ARENA_CODEX_API_KEY", '
                'wire_api = "responses" }' % OPENAI_COMPATIBLE_BASE_URL,
            ]
            env["ARENA_CODEX_API_KEY"] = provider.api_key
        else:
            command += [
                "--config", 'model_provider="arena"',
                "--config",
                'model_providers.arena={ name = "Arena", base_url = "%s", env_key = "ARENA_CODEX_API_KEY", '
                'wire_api = "chat" }' % provider.base_url,
            ]
            env["ARENA_CODEX_API_KEY"] = provider.api_key
        command += ["-"]  # read the prompt from stdin

        before = snapshot(workdir)

        expected = parse_deliverables(getattr(task, "expected_deliverables", ""))

        def _live_output(chunk: str) -> None:
            if provider.live_log_callback:
                provider.live_log_callback(_scrub(chunk, provider.api_key))

        async def invoke(run_command: list[str]):
            proc = await asyncio.create_subprocess_exec(
                *run_command,
                cwd=workdir,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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
            return proc, stdout_bytes, stderr_bytes, timed_out, completed_expected

        try:
            proc, stdout_bytes, stderr_bytes, timed_out, completed_expected = await invoke(command)
        except FileNotFoundError:
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log="",
                error_message=f"`{CODEX_BIN}` CLI not found on PATH.",
            )

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        fallback_used = False
        if not completed_expected and _image_input_unsupported(stdout_text):
            fallback_command = [*command[:-1], "--config", "tools_view_image=false", command[-1]]
            proc, stdout_bytes, stderr_bytes, timed_out, completed_expected = await invoke(fallback_command)
            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            fallback_used = True
        if timed_out:
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log="",
                error_message=f"Codex produced no new or updated deliverable for {TIMEOUT_SECONDS:.0f}s and was killed.",
            )

        del stderr_bytes

        saw_completed_turn, failure = _parse_events(stdout_text)
        if failure and not completed_expected:
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log=_scrub(stdout_text, provider.api_key)[-4000:],
                error_message=f"Codex failed: {failure}",
            )
        if proc.returncode != 0 and not completed_expected:
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log=_scrub(stdout_text, provider.api_key)[-4000:],
                error_message=f"Codex exited with status {proc.returncode}.",
            )
        if not saw_completed_turn and not completed_expected:
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log=_scrub(stdout_text, provider.api_key)[-4000:],
                error_message="Codex did not emit a completed turn.",
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
                error_message="Codex finished but produced no deliverable files.",
            )

        return RunResult(
            ok=True,
            deliverables=deliverables,
            raw_log=("Retried with image viewing disabled after provider rejected image input.\n" if fallback_used else "")
            + _scrub(stdout_text, provider.api_key)[-4000:],
        )
