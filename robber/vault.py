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
    "gemini": (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_KEY_1",
        "GEMINI_KEY_2",
        "GEMINI_KEY_3",
        "GEMINI_KEY_4",
    ),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "openrouter": (
        "OPENROUTER_API_KEY",
        "OPENROUTER_NEMOTRON_KEY",
        "OPENROUTER_OX_ALPHA_KEY",
    ),
    "opencode": ("OPENCODE_API_KEY", "OPENCODE_ZEN_KEY"),
    "mistral": ("MISTRAL_API_KEY",),
    "cohere": ("COHERE_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "litellm": ("LITELLM_MASTER_KEY",),
    "new_api": ("NEWAPI_API_KEY", "NEW_API_KEY"),
    "omniroute": ("OMNIROUTE_API_KEY", "OMNIROUTE_NAME1_KEY"),
    "tokenrouter": ("TOKENROUTER_API_KEY", "TOKEN_ROUTER_KEY_1", "TOKEN_ROUTER_KEY_2"),
    "github": ("GITHUB_TOKEN", "GH_TOKEN"),
    "gitlab": ("GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN"),
}

# Copy alias env names onto the rotator/LiteLLM canonical names when those are empty.
_CANONICAL_ALIASES: tuple[tuple[str, str], ...] = (
    ("GEMINI_KEY_1", "GEMINI_API_KEY"),
    ("GEMINI_KEY_2", "GEMINI_API_KEY_2"),
    ("GEMINI_KEY_3", "GEMINI_API_KEY_3"),
    ("GEMINI_KEY_4", "GEMINI_API_KEY_4"),
    ("OPENROUTER_NEMOTRON_KEY", "OPENROUTER_API_KEY"),
    ("OPENROUTER_OX_ALPHA_KEY", "OPENROUTER_API_KEY_2"),
    ("OMNIROUTE_NAME1_KEY", "OMNIROUTE_API_KEY"),
    ("TOKEN_ROUTER_KEY_1", "TOKENROUTER_API_KEY"),
    ("TOKEN_ROUTER_KEY_2", "TOKENROUTER_API_KEY_2"),
    ("OPENCODE_ZEN_KEY", "OPENCODE_API_KEY"),
)

LOCAL_PROVIDERS = frozenset({"ollama", "local", "vllm", "lmstudio", "llamacpp", "llama.cpp", "gguf"})
# ollama / vLLM / LM Studio do not use cloud API keys.

_PROVIDER_ALIASES = {
    "token_router": "tokenrouter",
    "google": "gemini",
    "google_ai_studio": "gemini",
}


def canonical_provider(provider: str) -> str:
    name = (provider or "").strip().lower()
    return _PROVIDER_ALIASES.get(name, name)


def read_text_loose(path: Path) -> str:
    """Read a text file without raising UnicodeDecodeError."""
    data = path.read_bytes()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", errors="replace")


def _promote_canonical_aliases() -> None:
    """Copy alias env names onto LiteLLM/rotator canonical names when empty."""
    for src, dest in _CANONICAL_ALIASES:
        if (os.environ.get(dest) or "").strip():
            continue
        value = (os.environ.get(src) or "").strip()
        if value:
            os.environ[dest] = value


def load_dotenv(path: Path | None = None) -> None:
    """Load repo `.env` into os.environ without overwriting process secrets."""
    target = ENV_PATH if path is None else path
    if not target.is_file():
        return
    try:
        text = read_text_loose(target)
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    _promote_canonical_aliases()


def iter_keys(provider: str) -> list[str]:
    """Return configured secrets for a provider, including `_2`..`_8` rotation slots."""
    load_dotenv()
    name = canonical_provider(provider)
    if name in LOCAL_PROVIDERS:
        return []
    found: list[str] = []
    seen: set[str] = set()
    names: list[str] = []
    for env_name in PROVIDER_ENV.get(name, ()):
        names.append(env_name)
        for index in range(2, 9):
            names.append(f"{env_name}_{index}")
    for env_name in names:
        value = os.environ.get(env_name, "").strip()
        if value and value not in seen:
            seen.add(value)
            found.append(value)
    return found


def get_key(provider: str) -> str | None:
    keys = iter_keys(provider)
    return keys[0] if keys else None


def rotate_key(provider: str, current: str | None = None) -> str | None:
    keys = iter_keys(provider)
    if not keys:
        return None
    if current is None or current not in keys:
        return keys[0]
    return keys[(keys.index(current) + 1) % len(keys)]


def providers_status() -> dict[str, bool]:
    load_dotenv()
    status = {name: get_key(name) is not None for name in PROVIDER_ENV}
    ollama_ready = False
    try:
        from robber.local.ollama_adapter import get_adapter

        ollama_ready = any(row.ok for row in get_adapter().endpoints)
    except Exception:
        ollama_ready = False
    if not ollama_ready:
        ollama_ready = bool(
            (os.environ.get("OLLAMA_API_BASE") or os.environ.get("OLLAMA_HOST") or "").strip()
        )
    status["ollama"] = ollama_ready
    return status


def configured_providers() -> list[str]:
    return [name for name, ok in providers_status().items() if ok]


def upsert_key(provider: str, secret: str) -> str:
    """Write a provider key to process env and .env. Never logs the value."""
    provider = canonical_provider(provider)
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
        try:
            lines = read_text_loose(ENV_PATH).splitlines()
        except OSError:
            lines = []
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
