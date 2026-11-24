"""Dynamic failover pool assembled from auto-discovered models."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from robber.engine.discovery import (
    QUARANTINE_SECONDS,
    DiscoveredModel,
    DiscoveryResult,
    load_cache,
    models_from_cache_row,
    resolve_endpoint_secret,
)
from robber.engine.model_ids import completion_model_ids
from robber.engine.protocols import PROTOCOL_NATIVE, PROTOCOL_OPENAI
from robber.router import Engine
from robber.config import PINNED_CLOUD_PROVIDERS
from robber.vault import canonical_provider, get_key

KeyResolver = Callable[[str], str]

_DEAD_MODEL_MARKERS = (
    "qwen2.5-coder",
    "qwen-2.5-coder",
    "huggingface",
    "hf.co/",
    "huggingface-instruct",
    "-tts",
    "preview-tts",
    "native-audio",
    "imagen",
    "veo",
    "embedding",
    "text-embedding",
)


def is_pinned_live_route(provider_id: str, model_id: str) -> bool:
    """True only for OpenRouter Nemotron, Groq Llama, and Gemini Flash chat."""
    pid = canonical_provider(provider_id)
    if pid == "google":
        pid = "gemini"
    if pid not in PINNED_CLOUD_PROVIDERS:
        return False
    blob = (model_id or "").strip().lower().replace("_", "-")
    if not blob:
        return False
    if any(marker in blob for marker in _DEAD_MODEL_MARKERS):
        return False
    if pid == "openrouter":
        return "nemotron" in blob
    if pid == "groq":
        if "guard" in blob:
            return False
        return "llama-3.3" in blob
    if pid == "gemini":
        if any(token in blob for token in ("tts", "audio", "image", "video", "lite", "latest")):
            return False
        return "gemini-2.5-flash" in blob or "gemini-2.0-flash" in blob
    return False

KeyResolver = Callable[[str], str]


@dataclass
class PoolMember:
    provider_id: str
    base_url: str
    model: DiscoveredModel
    headers: dict[str, str] = field(default_factory=dict)
    env_hint: str = ""
    api_key: str = ""


class FailoverPoolRegistry:
    def __init__(self) -> None:
        self._members: list[PoolMember] = []
        self._quarantine: dict[str, float] = {}
        self._key_resolver: KeyResolver | None = None

    def set_key_resolver(self, resolver: KeyResolver | None) -> None:
        self._key_resolver = resolver

    def ingest(
        self,
        result: DiscoveryResult,
        *,
        headers: dict[str, str] | None = None,
        env_hint: str = "",
        api_key: str = "",
    ) -> int:
        if not result.ok:
            return 0
        kept_keys = {item.base_url: item.api_key for item in self._members if item.api_key}
        self._members = [item for item in self._members if item.base_url != result.base_url]
        added = 0
        clean = {
            str(key): str(value)
            for key, value in (headers or {}).items()
            if str(key).lower() != "authorization"
        }
        for model in result.discovered_models:
            if model.disabled or not model.id:
                continue
            if not is_pinned_live_route(result.provider_id, model.id):
                continue
            self._members.append(
                PoolMember(
                    provider_id=result.provider_id,
                    base_url=result.base_url,
                    model=model,
                    headers=clean,
                    env_hint=env_hint,
                    api_key=api_key or kept_keys.get(result.base_url, ""),
                )
            )
            added += 1
        return added

    def hydrate_from_cache(self) -> int:
        from robber.engine.discovery import get_discovery

        data = load_cache(get_discovery().cache_path)
        overrides = data.get("overrides") or {}
        added = 0
        for base, row in (data.get("endpoints") or {}).items():
            if not isinstance(row, dict):
                continue
            models = models_from_cache_row(row, overrides)
            result = DiscoveryResult(str(row.get("provider_id") or "custom"), str(base), models)
            added += self.ingest(result)
        return added

    def quarantine(self, model_id: str, seconds: int = QUARANTINE_SECONDS) -> None:
        key = (model_id or "").strip()
        if key:
            self._quarantine[key] = time.monotonic() + max(1, seconds)

    def is_quarantined(self, model_id: str) -> bool:
        until = self._quarantine.get(model_id)
        if until is None:
            return False
        if time.monotonic() >= until:
            self._quarantine.pop(model_id, None)
            return False
        return True

    def note_latency(self, model_id: str, latency_ms: int) -> None:
        wanted = (model_id or "").strip()
        if not wanted:
            return
        for member in self._members:
            if member.model.id == wanted:
                member.model.latency_ms = max(0, int(latency_ms))

    def members_for(self, kind: str) -> list[PoolMember]:
        wanted = (kind or "general").strip().lower()
        live = [
            item
            for item in self._members
            if not item.model.disabled and not self.is_quarantined(item.model.id)
        ]
        primary = [item for item in live if item.model.type == wanted]
        rest = [item for item in live if item.model.type != wanted]
        primary.sort(key=self._rank, reverse=True)
        rest.sort(key=self._rank, reverse=True)
        return primary + rest

    def engines_for(self, kind: str, *, limit: int = 12) -> list[Engine]:
        engines: list[Engine] = []
        seen: set[str] = set()
        for member in self.members_for(kind):
            if member.model.id in seen:
                continue
            seen.add(member.model.id)
            engine = self._to_engine(member)
            if engine is None or not engine.live:
                continue
            if not is_pinned_live_route(engine.provider, engine.model_string or engine.label):
                continue
            engines.append(engine)
            if len(engines) >= limit:
                break
        return engines

    def next_after(self, current: Engine, kind: str, used: set[str] | None = None) -> Engine | None:
        skip = {item for item in (used or set()) if item}
        skip.add(current.label)
        if current.model_string:
            skip.add(current.model_string)
        for item in self.engines_for(kind, limit=24):
            if item.label in skip or (item.model_string and item.model_string in skip):
                continue
            return item
        return None

    def public(self) -> dict[str, Any]:
        active = [item.model.id for item in self._members if not item.model.disabled]
        quarantined = [key for key in list(self._quarantine) if self.is_quarantined(key)]
        return {
            "size": len(active),
            "quarantined": quarantined,
            "reasoning": [item.model.id for item in self.members_for("reasoning")[:8]],
            "coding": [item.model.id for item in self.members_for("coding")[:8]],
            "general": [item.model.id for item in self.members_for("general")[:8]],
        }

    def _rank(self, member: PoolMember) -> tuple[int, int, int, int]:
        model = member.model
        size = _size_score(model.id)
        return (
            1 if model.priority_high else 0,
            1 if model.is_free else 0,
            size,
            -int(model.latency_ms or 0),
        )

    def _to_engine(self, member: PoolMember) -> Engine | None:
        provider = canonical_provider(member.provider_id)
        if provider == "google":
            provider = "gemini"
        secret = member.api_key.strip()
        if not secret:
            if self._key_resolver is not None:
                secret = self._key_resolver(member.provider_id) or ""
            if not secret:
                secret = resolve_endpoint_secret(member.provider_id, member.env_hint)
            if not secret:
                secret = get_key(provider) or ""
        if not secret:
            return None
        model_id = member.model.id
        if not is_pinned_live_route(provider, model_id):
            return None
        if provider == "gemini":
            litellm = model_id if model_id.startswith("gemini/") else f"gemini/{model_id}"
            return Engine(
                model_id,
                litellm,
                "gemini",
                secret,
                None,
                dict(member.headers) or None,
                None,
                "normal",
                PROTOCOL_NATIVE,
                model_id,
            )
        http_model, litellm = completion_model_ids(
            model_string=model_id,
            litellm_model=None,
            api_base=member.base_url,
        )
        if provider == "groq":
            if not litellm.startswith("groq/"):
                litellm = f"groq/{http_model or model_id}"
            return Engine(
                model_id,
                litellm,
                "groq",
                secret,
                member.base_url or None,
                dict(member.headers) or None,
                None,
                "normal",
                PROTOCOL_OPENAI,
                http_model or model_id,
            )
        slug = model_id if model_id.startswith("openrouter/") else f"openrouter/{model_id}"
        return Engine(
            model_id,
            slug,
            "openrouter",
            secret,
            member.base_url or None,
            dict(member.headers) or None,
            None,
            "normal",
            PROTOCOL_OPENAI,
            model_id,
        )


def _size_score(model_id: str) -> int:
    lowered = (model_id or "").lower()
    for token, score in (
        ("405b", 40),
        ("235b", 36),
        ("120b", 32),
        ("70b", 30),
        ("32b", 24),
        ("27b", 22),
        ("14b", 16),
        ("8b", 10),
        ("7b", 8),
    ):
        if token in lowered:
            return score
    return 5


_POOL: FailoverPoolRegistry | None = None


def get_failover_pool() -> FailoverPoolRegistry:
    global _POOL
    if _POOL is None:
        _POOL = FailoverPoolRegistry()
    return _POOL


def bind_failover_pool(pool: FailoverPoolRegistry) -> FailoverPoolRegistry:
    global _POOL
    _POOL = pool
    return _POOL
