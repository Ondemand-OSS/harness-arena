"""Common harness adapter interfaces."""
from __future__ import annotations

import dataclasses
from typing import Callable, Protocol


@dataclasses.dataclass
class ProviderSettings:
    model: str
    base_url: str
    api_key: str
    reasoning_effort: str = ""
    ondemand_api_key: str = ""
    ondemand_endpoint_id: str = ""
    ondemand_reasoning_effort: str = ""
    # Admin toggle (routers/ondemand_models.py's suggest-plugins setting) —
    # see harnesses/ondemand.py's _suggest_plugin_ids for why this defaults
    # off.
    ondemand_suggest_plugins_enabled: bool = False
    # User-selected OnDemand skill ids (see ondemand_skills.py) to extract
    # into every harness's workdir before it runs — not OnDemand-specific
    # execution despite the name, just sourced from OnDemand's skill store.
    ondemand_skill_ids: list[str] = dataclasses.field(default_factory=list)
    # Names of the skills actually extracted into this run's workdir (see
    # runner.py / ondemand_skills.download_and_extract_skills) — set by the
    # runner after extraction, read by every non-OnDemand adapter's own
    # build_prompt() call to tell the agent the folder exists.
    workdir_skill_names: list[str] = dataclasses.field(default_factory=list)
    ondemand_session_callback: Callable[[str], None] | None = None
    # Receives scrubbed, incremental answer text while an OnDemand SSE query
    # is still in progress. The runner persists a throttled rolling tail for
    # the admin-only run monitor; adapters must never send secrets here.
    ondemand_log_callback: Callable[[str], None] | None = None
    # Same idea as ondemand_log_callback, but generic to every harness — the
    # runner sets this for every run, and CLI adapters feed it scrubbed
    # stdout/stderr chunks as their subprocess produces them (see
    # harnesses/_collect.py's communicate_with_deliverable_timeout).
    live_log_callback: Callable[[str], None] | None = None


@dataclasses.dataclass
class RunResult:
    """Result returned by a harness adapter."""

    ok: bool
    deliverables: list[str]
    raw_log: str
    error_message: str = ""
    ondemand_session_id: str = ""
    # Set when the harness deployed the app ITSELF and handed back a live
    # URL (OnDemand does this for web tasks — see ondemand.py). Reused
    # instead of paying to deploy the same code a second time; when it
    # expires we redeploy from the stored files like any other harness.
    preview_url: str = ""
    preview_sandbox_id: str = ""


class HarnessAdapter(Protocol):
    key: str
    name: str
    tagline: str
    async def run(self, task, workdir: str, provider: ProviderSettings) -> RunResult:
        """Execute the task in the given (already-created, empty) workdir."""
        ...
