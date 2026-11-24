"""Canonical model IDs for OpenAI-compatible providers (Groq, etc.)."""

from __future__ import annotations

GROQ_ALIASES: tuple[tuple[str, str], ...] = (
    ("qwen-2.5-coder-32b-instruct", "qwen/qwen3-32b"),
    ("qwen2.5-coder-32b-instruct", "qwen/qwen3-32b"),
    ("qwen-2.5-coder", "qwen/qwen3-32b"),
    ("qwen2.5-coder", "qwen/qwen3-32b"),
    ("llama-3.3-70b-versatile", "llama-3.3-70b-versatile"),
    ("llama-3.3-70b", "llama-3.3-70b-versatile"),
    ("llama3.3-70b", "llama-3.3-70b-versatile"),
    ("llama-3.3", "llama-3.3-70b-versatile"),
    ("llama3.3", "llama-3.3-70b-versatile"),
)


def strip_openai_prefix(model_id: str) -> str:
    text = (model_id or "").strip()
    if text.startswith("openai/"):
        return text.split("/", 1)[-1]
    if text.startswith("groq/"):
        return text.split("/", 1)[-1]
    return text


def is_groq_endpoint(api_base: str | None, litellm_model: str | None = None) -> bool:
    base = (api_base or "").lower()
    litellm = (litellm_model or "").lower()
    return "groq.com" in base or litellm.startswith("groq/")


def canonicalize_groq_model(model_id: str) -> str:
    raw = strip_openai_prefix(model_id).lower()
    for needle, mapped in GROQ_ALIASES:
        if needle in raw:
            return mapped
    return strip_openai_prefix(model_id)


def completion_model_ids(
    *,
    model_string: str,
    litellm_model: str | None,
    api_base: str | None,
) -> tuple[str, str]:
    """Return `(http_body_model, litellm_model)` for a completion call."""
    http_model = strip_openai_prefix(model_string or litellm_model or "")
    litellm = litellm_model or model_string or ""
    if is_groq_endpoint(api_base, litellm):
        http_model = canonicalize_groq_model(model_string or litellm)
        litellm = f"groq/{http_model}"
    return http_model, litellm
