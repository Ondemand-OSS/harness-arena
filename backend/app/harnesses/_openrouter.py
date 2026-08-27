"""OpenRouter helpers shared by the CLI adapters."""
from __future__ import annotations

ANTHROPIC_COMPATIBLE_BASE_URL = "https://openrouter.ai/api"
OPENAI_COMPATIBLE_BASE_URL = "https://openrouter.ai/api/v1"

_VENDOR_HINTS = {
    "deepseek": "deepseek",
    "qwen": "qwen",
    "llama": "meta-llama",
    "mistral": "mistralai",
    "mixtral": "mistralai",
    "gemini": "google",
    "grok": "x-ai",
    "gpt-": "openai",
    "o1": "openai",
    "o3": "openai",
    "claude": "anthropic",
}


def is_openrouter(base_url: str) -> bool:
    return "openrouter.ai" in (base_url or "").lower()


def normalize_model(model: str) -> str:
    """Only ever called once `is_openrouter(base_url)` is already true."""
    if not model or "/" in model:
        return model
    slug = model.strip().lower().replace(" ", "-")
    for hint, vendor in _VENDOR_HINTS.items():
        if hint in slug:
            return f"{vendor}/{slug}"
    return slug
