"""Bring-your-own-harness adapter: calls a remote HTTP service instead of
spawning a local CLI.

Request sent to `webhook_url` (POST, JSON):
    {
      "task_id": "...", "title": "...", "category": "...",
      "prompt": "...", "system_prompt": "...",
      "expected_deliverables": "...", "reference_files": "...",
      "reference_file_attachments": [{"filename": "...", "content_base64": "..."}],
      "provider": {"model": "...", "base_url": "...", "api_key": "..."}
    }
`reference_file_attachments` carries actual reference-file bytes when any
were attached to the task (see routers/tasks.py's reference-files
endpoints) — empty for the common case of a task with none, in which case
`reference_files` text is all a custom harness has to go on, same as
before this existed.
The provider block carries the SAME model/provider config every other
harness in this arena run is using — sending it is what keeps the
comparison fair (a custom harness that quietly used a different/stronger
model wouldn't be a harness comparison anymore).

Expected response (JSON):
    {"ok": true, "deliverables": [{"filename": "report.md", "content_base64": "..."}]}
    {"ok": false, "error": "human-readable reason"}

Each deliverable's decoded bytes are written into `workdir` under its
`filename`, then handled by the same directory-based collection the runner
already uses for every other harness — from the runner's point of view
this adapter looks identical to a local CLI one; the HTTP round-trip is
entirely hidden inside `run()`.
"""
from __future__ import annotations

import base64
import binascii
import os

import httpx

from ._reference_files import reference_file_blobs
from .base import ProviderSettings, RunResult

TIMEOUT_SECONDS = 300.0


def _safe_filename(name: str) -> str | None:
    """Rejects anything that could escape the run's workdir. A custom
    harness is, by definition, someone else's code responding to our
    request — its response is untrusted input, not just its request."""
    if not name or name in (".", "..") or "/" in name or "\\" in name or name.startswith("."):
        return None
    return name


class WebhookAdapter:

    def __init__(self, key: str, name: str, tagline: str, webhook_url: str, auth_header: str, auth_token: str):
        self.key = key
        self.name = name
        self.tagline = tagline
        self.webhook_url = webhook_url
        self.auth_header = auth_header
        self.auth_token = auth_token

    async def run(self, task, workdir: str, provider: ProviderSettings) -> RunResult:
        payload = {
            "task_id": task.id_aa,
            "title": task.title,
            "category": task.category,
            "prompt": task.prompt,
            "system_prompt": task.system_prompt,
            "expected_deliverables": task.expected_deliverables,
            "reference_files": task.reference_files,
            "reference_file_attachments": [
                {
                    "filename": os.path.basename(blob.get("filename") or ""),
                    "content_base64": base64.b64encode(blob.get("content") or b"").decode("ascii"),
                }
                for blob in reference_file_blobs(task)
                if blob.get("filename") and blob.get("content")
            ],
            "provider": {
                "model": provider.model,
                "base_url": provider.base_url,
                "api_key": provider.api_key,
            },
        }
        headers = {self.auth_header: self.auth_token} if self.auth_token else {}

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                resp = await client.post(self.webhook_url, json=payload, headers=headers)
        except httpx.RequestError as exc:
            return RunResult(ok=False, deliverables=[], raw_log="", error_message=f"webhook request failed: {exc}")

        if resp.status_code != 200:
            return RunResult(
                ok=False,
                deliverables=[],
                raw_log=resp.text[:2000],
                error_message=f"webhook returned HTTP {resp.status_code}",
            )

        try:
            body = resp.json()
        except ValueError:
            return RunResult(ok=False, deliverables=[], raw_log=resp.text[:2000], error_message="webhook response was not valid JSON")

        if not body.get("ok"):
            return RunResult(ok=False, deliverables=[], raw_log="", error_message=body.get("error") or "webhook reported failure")

        filenames: list[str] = []
        for item in body.get("deliverables", []):
            raw_name = item.get("filename")
            name = _safe_filename(raw_name) if isinstance(raw_name, str) else None
            if name is None:
                continue  # silently skip unsafe/malformed entries rather than failing the whole run
            try:
                content = base64.b64decode(item.get("content_base64", ""), validate=True)
            except (binascii.Error, ValueError):
                continue
            with open(os.path.join(workdir, name), "wb") as f:
                f.write(content)
            filenames.append(name)

        return RunResult(ok=True, deliverables=filenames, raw_log="webhook run complete")
