"""Auto-discover models from OpenAI-compatible `/v1/models` endpoints."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from robber.jail import workspace_root

CACHE_NAME = "models_cache.json"
CACHE_DIR = ".agent_robber"
CACHE_TTL_SECONDS = 24 * 60 * 60
QUARANTINE_SECONDS = 5 * 60
MAX_CACHED_MODELS = 200

REASONING_MARKERS = ("r1", "reasoning", "thinking", "o1", "nemotron", "qwq", "r0")
CODING_MARKERS = ("coder", "qwen-coder", "claude-sonnet", "deepseek-coder", "codellama", "starcoder", "codestral")
GENERAL_MARKERS = ("llama", "gpt-4o", "gpt-4", "mistral", "gemini", "qwen", "glm", "phi")

Fetcher = Callable[[str, dict[str, str]], Any]

_SVC: ModelDiscoveryService | None = None


@dataclass
class DiscoveredModel:
    id: str
    is_free: bool
    type: str
    scope: str | None = None
    latency_ms: int = 0
    context_length: int = 0
    disabled: bool = False
    priority_high: bool = False

    def public(self) -> dict[str, Any]:
        row = {
            "id": self.id,
            "is_free": self.is_free,
            "type": self.type,
        }
        if self.scope:
            row["scope"] = self.scope
        if self.priority_high:
            row["priority_high"] = True
        if self.disabled:
            row["disabled"] = True
        return row


@dataclass
class DiscoveryResult:
    provider_id: str
    base_url: str
    discovered_models: list[DiscoveredModel] = field(default_factory=list)
    ok: bool = True
    error: str = ""
    from_cache: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "base_url": self.base_url,
            "discovered_models": [item.public() for item in self.discovered_models if not item.disabled],
            "ok": self.ok,
            "error": self.error,
            "from_cache": self.from_cache,
        }

    @property
    def active_count(self) -> int:
        return len([item for item in self.discovered_models if not item.disabled])


def discovery_log(count: int) -> str:
    return f"✦ Initiating: Discovered {count} active models from endpoint"


def failover_log(failed: str, swapped: str) -> str:
    return (
        f"Auto-detected failure on [{failed}] -> "
        f"Swapped to auto-discovered [{swapped}] in failover pool"
    )


def models_endpoint(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("base_url is empty")
    if base.endswith("/models"):
        return base
    if re.search(r"/v\d+$", base) or base.endswith("/openai") or "/openai/v1" in base:
        return f"{base}/models"
    return f"{base}/v1/models"


def classify_model_id(model_id: str, *, pricing: dict[str, Any] | None = None) -> DiscoveredModel:
    raw = (model_id or "").strip()
    lowered = raw.lower()
    is_free = _is_free(lowered, pricing)
    kind = "general"
    if any(marker in lowered for marker in REASONING_MARKERS):
        kind = "reasoning"
    elif any(marker in lowered for marker in CODING_MARKERS):
        kind = "coding"
    elif any(marker in lowered for marker in GENERAL_MARKERS):
        kind = "general"
    scope = "coder" if kind == "coding" else None
    return DiscoveredModel(id=raw, is_free=is_free, type=kind, scope=scope)


def _is_free(lowered_id: str, pricing: dict[str, Any] | None) -> bool:
    if ":free" in lowered_id or lowered_id.endswith("-free") or "/free" in lowered_id:
        return True
    if not pricing:
        return False
    if pricing.get("prompt_cost") in {0, 0.0, "0", "0.0"}:
        return True
    prompt = pricing.get("prompt")
    try:
        prompt_n = float(prompt) if prompt is not None else None
    except (TypeError, ValueError):
        return False
    return prompt_n == 0


def parse_models_payload(payload: Any) -> list[DiscoveredModel]:
    rows: list[Any]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            rows = data
        elif isinstance(payload.get("models"), list):
            rows = payload["models"]
        else:
            rows = []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    found: list[DiscoveredModel] = []
    seen: set[str] = set()
    for item in rows:
        model_id = ""
        pricing: dict[str, Any] | None = None
        if isinstance(item, str):
            model_id = item.strip()
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
            pricing = dict(item.get("pricing") or {}) if isinstance(item.get("pricing"), dict) else {}
            if "prompt_cost" in item and "prompt_cost" not in pricing:
                pricing["prompt_cost"] = item.get("prompt_cost")
            if "prompt" in item and "prompt" not in pricing:
                pricing["prompt"] = item.get("prompt")
            if not pricing:
                pricing = None
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        found.append(classify_model_id(model_id, pricing=pricing))
    return cap_models(found)


def cap_models(models: list[DiscoveredModel], limit: int = MAX_CACHED_MODELS) -> list[DiscoveredModel]:
    free = [item for item in models if item.is_free]
    paid = [item for item in models if not item.is_free]
    return (free + paid)[: max(1, limit)]


def _cache_path() -> Path:
    return workspace_root() / CACHE_DIR / CACHE_NAME


def load_cache(path: Path | None = None) -> dict[str, Any]:
    target = path or _cache_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"endpoints": {}, "overrides": {}}
    if not isinstance(raw, dict):
        return {"endpoints": {}, "overrides": {}}
    raw.setdefault("endpoints", {})
    raw.setdefault("overrides", {})
    return raw


def save_cache(data: dict[str, Any], path: Path | None = None) -> None:
    target = path or _cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = {"endpoints": data.get("endpoints") or {}, "overrides": data.get("overrides") or {}}
    blob = json.dumps(clean, indent=2)
    if re.search(r"(api[_-]?key|authorization|bearer\s+sk-)", blob, re.I):
        raise ValueError("refusing to write secrets into models_cache.json")
    target.write_text(blob, encoding="utf-8")


def apply_overrides(models: list[DiscoveredModel], overrides: dict[str, Any]) -> list[DiscoveredModel]:
    for item in models:
        spec = overrides.get(item.id) if isinstance(overrides, dict) else None
        if not isinstance(spec, dict):
            continue
        item.disabled = spec.get("disabled") is True
        item.priority_high = spec.get("priority_high") is True
    return models


def models_from_cache_row(row: dict[str, Any], overrides: dict[str, Any] | None = None) -> list[DiscoveredModel]:
    models = [
        DiscoveredModel(
            id=str(item.get("id") or ""),
            is_free=bool(item.get("is_free")),
            type=str(item.get("type") or "general"),
            scope=str(item["scope"]) if item.get("scope") else None,
            latency_ms=int(item.get("latency_ms") or 0),
        )
        for item in (row.get("discovered_models") or [])
        if isinstance(item, dict) and item.get("id")
    ]
    apply_overrides(models, overrides or {})
    return models


def resolve_endpoint_secret(provider_id: str, env_hint: str = "") -> str:
    from robber.engine.presets import get_preset
    from robber.vault import get_key, get_named

    pid = (provider_id or "").strip().lower()
    hint = (env_hint or "").strip()
    mapped = {
        "openrouter": "openrouter",
        "groq": "groq",
        "openai": "openai",
        "anthropic": "anthropic",
        "gemini": "gemini",
        "google": "gemini",
        "google_ai_studio": "gemini",
        "mistral": "mistral",
        "cohere": "cohere",
        "together": "together",
        "deepseek": "deepseek",
        "opencode": "opencode",
        "litellm": "litellm",
        "new_api": "new_api",
        "newapi": "new_api",
        "omniroute": "omniroute",
        "token_router": "tokenrouter",
        "tokenrouter": "tokenrouter",
    }
    secret = get_key(mapped.get(pid, pid))
    if secret:
        return secret
    if hint:
        named = get_named(hint)
        if named:
            return named
    try:
        preset = get_preset(provider_id)
        preset_hint = str(preset.get("env_hint") or "")
        if preset_hint:
            named = get_named(preset_hint)
            if named:
                return named
            env_val = os.environ.get(preset_hint, "").strip()
            if env_val:
                return env_val
    except Exception:
        pass
    upper = pid.upper().replace("-", "_") + "_API_KEY"
    return os.environ.get(upper, "").strip()


def public_headers(headers: dict[str, str] | None) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in (headers or {}).items()
        if str(key).lower() != "authorization"
    }


class ModelDiscoveryService:
    def __init__(self, cache_path: Path | None = None, fetcher: Fetcher | None = None) -> None:
        self.cache_path = cache_path
        self.fetcher = fetcher
        self._last: dict[str, DiscoveryResult] = {}

    def _store(self) -> dict[str, Any]:
        return load_cache(self.cache_path)

    def set_override(self, model_id: str, *, disabled: bool | None = None, priority_high: bool | None = None) -> None:
        data = self._store()
        overrides = data.setdefault("overrides", {})
        row = dict(overrides.get(model_id) or {})
        if disabled is not None:
            row["disabled"] = bool(disabled)
        if priority_high is not None:
            row["priority_high"] = bool(priority_high)
        overrides[model_id] = row
        save_cache(data, self.cache_path)
        for result in self._last.values():
            apply_overrides(result.discovered_models, overrides)

    def public_summary(self) -> dict[str, Any]:
        data = self._store()
        endpoints = []
        total = 0
        for base, row in (data.get("endpoints") or {}).items():
            if not isinstance(row, dict):
                continue
            models = row.get("discovered_models") or []
            count = len(models) if isinstance(models, list) else 0
            total += count
            endpoints.append(
                {
                    "provider_id": row.get("provider_id") or "",
                    "base_url": base,
                    "count": count,
                    "fetched_at": row.get("fetched_at") or 0,
                }
            )
        return {"endpoints": endpoints, "models": total, "overrides": len(data.get("overrides") or {})}

    async def discover(
        self,
        *,
        provider_id: str,
        base_url: str,
        api_key: str = "",
        extra_headers: dict[str, str] | None = None,
        force: bool = False,
    ) -> DiscoveryResult:
        base = (base_url or "").strip().rstrip("/")
        pid = (provider_id or "custom").strip().lower() or "custom"
        if not base:
            return DiscoveryResult(pid, "", ok=False, error="base_url is empty")
        data = self._store()
        cached = (data.get("endpoints") or {}).get(base)
        now = time.time()
        if not force and isinstance(cached, dict):
            fetched = float(cached.get("fetched_at") or 0)
            if now - fetched < CACHE_TTL_SECONDS:
                models = models_from_cache_row(cached, data.get("overrides") or {})
                result = DiscoveryResult(pid, base, models, from_cache=True)
                self._last[base] = result
                return result
        headers = public_headers(extra_headers)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            url = models_endpoint(base)
            payload = await self._fetch(url, headers)
            models = parse_models_payload(payload)
        except (httpx.HTTPError, ValueError, OSError, json.JSONDecodeError, TypeError) as exc:
            return DiscoveryResult(pid, base, ok=False, error=type(exc).__name__)
        apply_overrides(models, data.get("overrides") or {})
        data.setdefault("endpoints", {})[base] = {
            "provider_id": pid,
            "fetched_at": now,
            "discovered_models": [item.public() for item in models],
        }
        save_cache(data, self.cache_path)
        result = DiscoveryResult(pid, base, models)
        self._last[base] = result
        return result

    async def _fetch(self, url: str, headers: dict[str, str]) -> Any:
        if self.fetcher is not None:
            maybe = self.fetcher(url, headers)
            if hasattr(maybe, "__await__"):
                return await maybe  # type: ignore[misc]
            return maybe
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()


async def discover_and_ingest(
    *,
    provider_id: str,
    base_url: str,
    api_key: str = "",
    extra_headers: dict[str, str] | None = None,
    env_hint: str = "",
    force: bool = False,
) -> DiscoveryResult:
    from robber.engine.failover_pool import get_failover_pool

    result = await get_discovery().discover(
        provider_id=provider_id,
        base_url=base_url,
        api_key=api_key,
        extra_headers=extra_headers,
        force=force,
    )
    if result.ok:
        get_failover_pool().ingest(
            result,
            headers=public_headers(extra_headers),
            env_hint=env_hint,
            api_key=api_key,
        )
    return result


def bind_discovery(service: ModelDiscoveryService) -> ModelDiscoveryService:
    global _SVC
    _SVC = service
    return _SVC


def get_discovery() -> ModelDiscoveryService:
    global _SVC
    if _SVC is None:
        _SVC = ModelDiscoveryService()
    return _SVC
