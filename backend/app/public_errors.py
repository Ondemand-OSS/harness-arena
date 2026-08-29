from __future__ import annotations


def rate_limit_message(error_message: str) -> str | None:
    text = (error_message or "").lower()
    signals = ("rate limit", "rate_limit", "too many requests", "http 429", "status 429", "throttl")
    if any(signal in text for signal in signals):
        return "The model is rate-limited. Please retry after some time."
    return None


def no_deliverable_message(error_message: str) -> str | None:
    """This message (runner.py's `written_count == 0` checks) is
    synthesized by us, not the adapter/provider — unlike a raw stderr or
    provider error body, there's nothing sensitive in it, so unlike the
    rest of a genuine `error` status it's safe to show every viewer."""
    text = error_message or ""
    if text.startswith("Harness reported success but produced no deliverable files."):
        return text
    return None
