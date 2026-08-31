"""Hermes CLI harness adapter."""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

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
HERMES_BIN = os.environ.get("ARENA_HERMES_BIN", "hermes")

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


def _yaml_str(value: str) -> str:
    """Minimal YAML double-quoted scalar — escapes the two characters that
    would otherwise break out of the quotes. Avoids a PyYAML dependency for
    the one small config file this adapter writes."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class HermesAdapter:
    key = "hermes"
    name = "Hermes Agent"
    tagline = "Nous Research's self-improving terminal agent."

    async def run(self, task, workdir: str, provider: ProviderSettings) -> RunResult:
        if not provider.api_key:
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log="",
                error_message="No provider profile configured. Add an API key (or select a free-tier "
                "profile) in Setup before running Hermes.",
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
        openai_default = _is_openai_default(provider.base_url)
        model = normalize_model(provider.model) if via_openrouter else provider.model

        # Outside workdir, not a subdirectory of it: _collect.py walks the
        # whole workdir for deliverables, and Hermes' own sessions/logs/
        # memories would otherwise get swept up as "output files".
        hermes_home = tempfile.mkdtemp(prefix="hermes-home-")
        try:
            command = [
                HERMES_BIN,
                "chat",
                "--query-file", "-",
                # Was --quiet: per hermes's own docs that "suppress[es]
                # banner, spinner, and tool previews — only output the
                # final response and session info", which is exactly why
                # the live-log view (see runner.py's persist_live_log) saw
                # nothing but one startup line for the whole run. Nothing
                # here parses stdout's exact shape (see stdout_text below —
                # only ever used as a raw text tail, never structured), so
                # trading quiet's terseness for --verbose's tool-activity
                # detail costs nothing correctness-wise.
                "--verbose",
                "--yolo",
                "--ignore-rules",
            ]
            if model:
                command += ["--model", model]
            if provider.reasoning_effort:
                command += ["--reasoning", provider.reasoning_effort]

            env = os.environ.copy()
            env["HERMES_HOME"] = hermes_home
            if via_openrouter:
                env["OPENROUTER_API_KEY"] = provider.api_key
                command += ["--provider", "openrouter"]
            elif openai_default:
                env["OPENAI_API_KEY"] = provider.api_key
                command += ["--provider", "openai-api"]
            else:
                env["OPENAI_API_KEY"] = provider.api_key
                with open(os.path.join(hermes_home, "config.yaml"), "w") as f:
                    f.write(
                        "model:\n"
                        f"  default: {_yaml_str(model or '')}\n"
                        f"  base_url: {_yaml_str(provider.base_url)}\n"
                        f"  api_key: {_yaml_str(provider.api_key)}\n"
                        "provider: custom\n"
                    )
                command += ["--provider", "custom"]

            before = snapshot(workdir)
            expected = parse_deliverables(getattr(task, "expected_deliverables", ""))

            def _live_output(chunk: str) -> None:
                if provider.live_log_callback:
                    provider.live_log_callback(_scrub(chunk, provider.api_key))

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
                    error_message=f"`{HERMES_BIN}` CLI not found on PATH.",
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
                    error_message=f"Hermes produced no new or updated deliverable for {TIMEOUT_SECONDS:.0f}s and was killed.",
                )

            stdout_text = _scrub(stdout_bytes.decode("utf-8", errors="replace"), provider.api_key)
            stderr_text = _scrub(stderr_bytes.decode("utf-8", errors="replace"), provider.api_key)

            if proc.returncode != 0 and not completed_expected:
                detail = stderr_text[-1500:].strip() or stdout_text[-1500:].strip()
                return RunResult(
                    ok=False,
                    deliverables=[],
                    raw_log=(stdout_text + "\n" + stderr_text)[-4000:],
                    error_message=f"Hermes exited with status {proc.returncode}: {detail or 'no output'}",
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
                    error_message="Hermes finished but produced no deliverable files.",
                )

            return RunResult(
                ok=True,
                deliverables=deliverables,
                raw_log=stdout_text[-4000:],
            )
        finally:
            shutil.rmtree(hermes_home, ignore_errors=True)
