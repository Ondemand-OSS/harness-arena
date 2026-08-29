from __future__ import annotations


def rate_limit_message(error_message: str) -> str | None:
    text = (error_message or "").lower()
    signals = ("rate limit", "rate_limit", "too many requests", "http 429", "status 429", "throttl")
    if any(signal in text for signal in signals):
        return "The model is rate-limited. Please retry after some time."
    return None


def no_deliverable_message(error_message: str) -> str | None:
    """Every "success with nothing to show for it" message — runner.py's
    own centralized "Harness reported success but produced no deliverable
    files." check, and each adapter's own earlier "<Name> finished but
    produced no deliverable files." (see harnesses/claude_code.py,
    hermes_cli.py, openclaw_cli.py, opencode_cli.py, codex_cli.py) — is
    synthesized by us, not the adapter/provider. Unlike a raw stderr or
    provider error body, there's nothing sensitive in it, so unlike the
    rest of a genuine `error` status it's safe to show every viewer."""
    text = error_message or ""
    if text.endswith("no deliverable files."):
        return text
    return None
