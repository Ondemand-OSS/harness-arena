"""OpenClaw CLI harness adapter."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import uuid

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

TIMEOUT_SECONDS = float(os.environ.get("ARENA_HARNESS_TIMEOUT_SECONDS", "3600"))
OPENCLAW_BIN = os.environ.get("ARENA_OPENCLAW_BIN", "openclaw")
# Where the Docker image installs OpenClaw's private Node 22 (see
# backend/Dockerfile) — must be ahead of PATH's system Node when we spawn
# OpenClaw, or its `env node` shebang silently resolves the wrong Node.
_NODE22_BIN = os.environ.get("ARENA_OPENCLAW_NODE_BIN", "/opt/node22/bin")

# Registered as a custom provider in a per-run config (see run() below) — the
# docs' `agent exec` command with --isolated/--auth-env-only/--cwd does not
# exist in the installed CLI (verified against the real binary's --help);
# this is the config actually recognized by `openclaw agent --local`.
_PROVIDER_ID = "arena"
_OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"


def _scrub(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def _failure_detail(stderr_text: str) -> str:
    """The one line of stderr worth surfacing in the run's error message.

    Just taking the last line is wrong for OpenClaw's own preflight Node
    version guard: its multi-line advisory ends with an `nvm alias default
    24` command, not the actual "Node.js >=22... is required (current:
    vX)" line that explains what went wrong — so a version mismatch used
    to surface as an opaque, out-of-context nvm command instead of the
    reason. Prefer a line that actually names the problem when one is
    present; fall back to the last non-empty line otherwise."""
    lines = [line for line in stderr_text.strip().splitlines() if line.strip()]
    if not lines:
        return ""
    for line in lines:
        if "node.js" in line.lower() and "required" in line.lower():
            return line.strip()
    return lines[-1].strip()


class OpenClawAdapter:
    key = "openclaw"
    name = "OpenClaw"
    tagline = "Personal AI assistant, run headlessly via `agent --local`."

    async def run(self, task, workdir: str, provider: ProviderSettings) -> RunResult:
        if not provider.api_key:
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log="",
                error_message="No provider profile configured. Add an API key (or select a free-tier "
                "profile) in Setup before running OpenClaw.",
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
        base_url = provider.base_url or _OPENAI_DEFAULT_BASE_URL
        model_ref = f"{_PROVIDER_ID}/{model}"

        # Own state dir + config file per run, outside workdir: state must
        # not be shared across concurrent runs, and _collect.py walks the
        # whole workdir for deliverables (see hermes_cli.py's same fix).
        state_dir = tempfile.mkdtemp(prefix="openclaw-state-")
        try:
            config = {
                "models": {
                    "providers": {
                        _PROVIDER_ID: {
                            "baseUrl": base_url,
                            "apiKey": provider.api_key,
                            "api": "openai-completions",
                            # Without this, `--thinking` on an arbitrary
                            # custom model is flatly rejected ("Thinking
                            # level ... is not supported for ...") even
                            # when the admin explicitly set an effort in
                            # Setup — verified against the real CLI.
                            "models": [{"id": model, "name": model, "reasoning": True}],
                        }
                    }
                },
                # Documented for exactly this case: "Set this explicitly
                # when running from wrappers so path resolution stays
                # deterministic." There is no --cwd flag on `agent --local`.
                "agents": {"defaults": {"workspace": workdir}},
            }
            config_path = os.path.join(state_dir, "openclaw.json")
            with open(config_path, "w") as f:
                json.dump(config, f)

            # `--message-file -` is not stdin here (confirmed against the
            # real CLI: it does a literal file lookup and errors ENOENT on
            # "-") — the prompt has to be a real file.
            prompt_path = os.path.join(state_dir, "prompt.txt")
            with open(prompt_path, "w") as f:
                f.write(prompt)

            command = [
                OPENCLAW_BIN,
                "agent", "--local",
                "--message-file", prompt_path,
                "--session-key", f"arena-{uuid.uuid4().hex}",
                "--model", model_ref,
                # A little above our own external deliverable-timeout so
                # that one (which resets on file activity) governs normal
                # operation; this is a backstop, not the primary driver.
                "--timeout", str(int(TIMEOUT_SECONDS) + 60),
                "--json",
            ]
            if provider.reasoning_effort:
                command += ["--thinking", provider.reasoning_effort]

            env = os.environ.copy()
            env["OPENCLAW_STATE_DIR"] = state_dir
            env["OPENCLAW_CONFIG_PATH"] = config_path
            # Defense in depth: OpenClaw's npm-installed bin has a
            # `#!/usr/bin/env node` shebang, which re-resolves `node` from
            # PATH at *runtime*, not from wherever it was installed. The
            # Docker image installs it under a private Node 22 specifically
            # so it doesn't run under the system's older Node — but that
            # only holds if this private bin dir is actually on PATH when
            # we spawn it, which shouldn't depend solely on the image's own
            # wrapper script getting that right. _NODE22_BIN is a no-op
            # anywhere that directory doesn't exist (e.g. local dev).
            if os.path.isdir(_NODE22_BIN):
                env["PATH"] = f"{_NODE22_BIN}{os.pathsep}{env.get('PATH', '')}"

            before = snapshot(workdir)
            expected = parse_deliverables(getattr(task, "expected_deliverables", ""))

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
                    error_message=f"`{OPENCLAW_BIN}` CLI not found on PATH.",
                )

            try:
                # The prompt already went in via --message-file; stdin is
                # unused here, so this only drives the shared timeout loop.
                stdout_bytes, stderr_bytes, timed_out, completed_expected = await communicate_with_deliverable_timeout(
                    proc, "", workdir, before, TIMEOUT_SECONDS, expected,
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
                    error_message=f"OpenClaw produced no new or updated deliverable for {TIMEOUT_SECONDS:.0f}s and was killed.",
                )

            stdout_text = _scrub(stdout_bytes.decode("utf-8", errors="replace"), provider.api_key)
            stderr_text = _scrub(stderr_bytes.decode("utf-8", errors="replace"), provider.api_key)

            # Verified against the real CLI: on failure stdout is empty and
            # the reason is a plain line on stderr (no JSON envelope at
            # all); a JSON envelope only appears on stdout for a completed
            # run, so this doesn't try to parse one on the failure path.
            if proc.returncode != 0 and not completed_expected:
                detail = _failure_detail(stderr_text)
                return RunResult(
                    ok=False,
                    deliverables=[],
                    raw_log=(stdout_text + "\n" + stderr_text)[-4000:],
                    error_message=f"OpenClaw exited with status {proc.returncode}: {detail or 'no output'}",
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
                    error_message="OpenClaw finished but produced no deliverable files.",
                )

            return RunResult(
                ok=True,
                deliverables=deliverables,
                raw_log=stdout_text[-4000:],
            )
        finally:
            shutil.rmtree(state_dir, ignore_errors=True)
