from __future__ import annotations


def rate_limit_message(error_message: str) -> str | None:
    text = (error_message or "").lower()
    signals = ("rate limit", "rate_limit", "too many requests", "http 429", "status 429", "throttl")
    if any(signal in text for signal in signals):
        return "The model is rate-limited. Please retry after some time."
    return None
