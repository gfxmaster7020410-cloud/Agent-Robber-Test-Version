"""BYOK key vault.

Reads provider keys from the process environment and an optional local `.env`
file. Never logs or returns the secret values themselves — only booleans and
the key when a router call actually needs it.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

PROVIDER_ENV: dict[str, tuple[str, ...]] = {
    "groq": ("GROQ_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "opencode": ("OPENCODE_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "cohere": ("COHERE_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
}


def load_dotenv(path: Path = ENV_PATH) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_key(provider: str) -> str | None:
    for name in PROVIDER_ENV.get(provider, ()):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def providers_status() -> dict[str, bool]:
    return {name: get_key(name) is not None for name in PROVIDER_ENV}


def configured_providers() -> list[str]:
    return [name for name, ok in providers_status().items() if ok]


def upsert_key(provider: str, secret: str) -> str:
    """Write a provider key to process env and .env. Never logs the value."""
    provider = provider.strip().lower()
    secret = secret.strip()
    names = PROVIDER_ENV.get(provider)
    if not names:
        raise ValueError(f"unknown provider '{provider}'")
    if not secret:
        raise ValueError("key is empty")
    env_name = names[0]
    os.environ[env_name] = secret
    _upsert_env_line(env_name, secret)
    return env_name


def _upsert_env_line(name: str, value: str) -> None:
    lines: list[str] = []
    if ENV_PATH.is_file():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    replaced = False
    next_lines: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("#") or "=" not in raw:
            next_lines.append(raw)
            continue
        key, _, _rest = raw.partition("=")
        if key.strip() == name:
            next_lines.append(f"{name}={value}")
            replaced = True
        else:
            next_lines.append(raw)
    if not replaced:
        if next_lines and next_lines[-1].strip():
            next_lines.append("")
        next_lines.append(f"{name}={value}")
    ENV_PATH.write_text("\n".join(next_lines) + "\n", encoding="utf-8")


def get_named(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def upsert_named(name: str, secret: str) -> str:
    """Write an arbitrary env key (custom model secrets). Never logs the value."""
    env_name = name.strip()
    secret = secret.strip()
    if not env_name or not env_name.replace("_", "").isalnum() or env_name[0].isdigit():
        raise ValueError("invalid secret env name")
    if not secret:
        raise ValueError("key is empty")
    os.environ[env_name] = secret
    _upsert_env_line(env_name, secret)
    return env_name
