"""LLM orchestration — presets, protocol adapters, combo ensembles, failover."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from robber.db.database import Database
from robber.engine.discovery import DiscoveryResult, discover_and_ingest, resolve_endpoint_secret
from robber.engine.failover_pool import get_failover_pool, is_pinned_live_route
from robber.engine.model_ids import completion_model_ids
from robber.engine.presets import (
    PRESETS,
    batch_to_tier,
    get_preset,
    normalize_tier,
    preset_public,
    tier_to_batch,
)
from robber.engine.protocols import (
    PROTOCOL_ANTHROPIC,
    PROTOCOL_NATIVE,
    PROTOCOL_OPENAI,
    ping_endpoint,
)
from robber.router import Engine, register_route
from robber.vault import get_key

REASONING_RE = re.compile(
    r"\b(architect|architecture|design|reason|why|plan|trade-?off|algorithm|"
    r"complexity|spec|rfc|compare|critique)\b",
    re.I,
)
CODING_RE = re.compile(
    r"\b(refactor|implement|function|class|patch|diff|typescript|python|bug|"
    r"fix|code|module|component|typeerror|compile|test)\b",
    re.I,
)

TaskKind = str  # reasoning | coding | general

FREE_ROUTE_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "id": "openrouter-nemotron-35-free",
        "name": "OpenRouter Nemotron 3.5 Lightning",
        "provider_preset": "OPENROUTER",
        "model_string": "nvidia/nemotron-3.5-lightning:free",
        "context_length": 1_000_000,
        "cost_per_1m": 0.0,
        "api_key": "",
        "is_active": True,
    },
    {
        "id": "omniroute-nemotron-35",
        "name": "OmniRoute Nemotron 3.5",
        "provider_preset": "OMNIROUTE",
        "model_string": "nvidia/nemotron-3.5-lightning:free",
        "context_length": 128000,
        "cost_per_1m": 0.0,
        "api_key": "",
        "is_active": True,
    },
    {
        "id": "tokenrouter-primary",
        "name": "Token Router",
        "provider_preset": "TOKEN_ROUTER",
        "model_string": "gpt-4o-mini",
        "context_length": 128000,
        "cost_per_1m": 0.0,
        "api_key": "",
        "is_active": True,
    },
    {
        "id": "groq-llama-33",
        "name": "Groq Llama 3.3",
        "provider_preset": "GROQ",
        "model_string": "llama-3.3-70b-versatile",
        "context_length": 128000,
        "cost_per_1m": 0.0,
        "api_key": "",
        "is_active": True,
    },
    {
        "id": "gemini-25-flash",
        "name": "Gemini 2.5 Flash",
        "provider_preset": "GEMINI",
        "model_string": "gemini-2.5-flash",
        "context_length": 1_000_000,
        "cost_per_1m": 0.0,
        "api_key": "",
        "is_active": True,
    },
)


class CustomModelManager:
    """Async orchestration manager. API keys never leave the process in logs."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.active_batch: str = "normal"
        self._rr: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self.models_cache: list[dict[str, Any]] = []
        self.combos_cache: list[dict[str, Any]] = []
        self.last_discovery: DiscoveryResult | None = None

    @property
    def route_mode(self) -> str:
        return batch_to_tier(self.active_batch)

    def set_batch_mode(self, mode: str) -> str:
        tier = normalize_tier(mode)
        self.active_batch = tier_to_batch(tier)
        return tier

    def set_route_mode(self, mode: str) -> str:
        return self.set_batch_mode(mode)

    async def get_models(self, active_only: bool = False) -> list[dict[str, Any]]:
        return await self.db.get_models(active_only=active_only)

    async def add_model(self, model_data: dict[str, Any]) -> str:
        entry_id = await self.db.add_model(model_data)
        await self._sync_route(entry_id)
        await self.refresh_cache()
        self.last_discovery = await self.discover_from_row(
            entry_id,
            api_key=str(model_data.get("api_key") or ""),
            force=True,
        )
        return entry_id

    async def update_model(self, model_id: str, updates: dict[str, Any]) -> bool:
        ok = await self.db.update_model(model_id, updates)
        if ok:
            await self._sync_route(model_id)
            await self.refresh_cache()
            if any(key in updates for key in ("base_url", "api_key", "custom_headers", "provider_preset")):
                self.last_discovery = await self.discover_from_row(
                    model_id,
                    api_key=str(updates.get("api_key") or ""),
                    force=True,
                )
        return ok

    async def delete_model(self, model_id: str) -> bool:
        ok = await self.db.delete_model(model_id)
        if ok:
            await self.refresh_cache()
        return ok

    async def create_combo_model(self, combo_data: dict[str, Any]) -> str:
        combo_id = await self.db.create_combo_model(combo_data)
        await self.refresh_cache()
        return combo_id

    async def get_combo_models(self) -> list[dict[str, Any]]:
        return await self.db.get_combo_models()

    async def update_combo_model(self, combo_id: str, updates: dict[str, Any]) -> bool:
        ok = await self.db.update_combo_model(combo_id, updates)
        if ok:
            await self.refresh_cache()
        return ok

    async def refresh_cache(self) -> None:
        self.models_cache = await self.db.get_models()
        self.combos_cache = await self.db.get_combo_models()

    def presets(self) -> list[dict[str, Any]]:
        return preset_public()

    def classify_task(self, prompt: str, mode: str = "act") -> TaskKind:
        from robber.intent import is_inquiry

        if (mode or "").strip().lower() == "plan":
            return "reasoning"
        if is_inquiry(prompt):
            return "general"
        text = prompt or ""
        if REASONING_RE.search(text):
            return "reasoning"
        if CODING_RE.search(text):
            return "coding"
        return "general"

    async def scan_ollama_models(self, host: str = "http://127.0.0.1:11434") -> list[str]:
        from robber.health import scan_ollama_tags

        try:
            return await scan_ollama_tags(host)
        except Exception:
            return []

    async def test_model(self, model_id: str) -> dict[str, Any]:
        row = await self.db.get_model_row(model_id)
        if row is None:
            raise ValueError("model not found")
        public = self.db._public_model(row)
        secret = await self._secret_for(row)
        protocol = str(row.get("protocol_type") or PROTOCOL_OPENAI)
        base = str(row.get("base_url") or "")
        if protocol == PROTOCOL_NATIVE and not base:
            ok = bool(secret) or public["provider_preset"] in {"OLLAMA", "GEMINI", "VERTEX"}
            latency = 1 if ok else 0
            await self.db.set_model_latency(model_id, latency)
            await self.refresh_cache()
            return {"ok": ok, "latency_ms": latency, "error": "" if ok else "key missing"}
        ok, latency, error = await ping_endpoint(
            protocol=protocol,
            base_url=base,
            api_key=secret,
            extra_headers=public["custom_headers"],
        )
        if ok:
            await self.db.set_model_latency(model_id, latency)
            await self.refresh_cache()
        return {"ok": ok, "latency_ms": latency, "error": error}

    async def engines_for_prompt(
        self,
        prompt: str,
        *,
        mode: str = "act",
        tier: str | None = None,
        limit: int = 4,
    ) -> list[Engine]:
        wanted = normalize_tier(tier or self.route_mode)
        combo = await self.db.get_combo_for_tier(wanted)
        kind = self.classify_task(prompt, mode)
        ordered_ids = await self._pool_for(combo, kind) if combo is not None else []
        engines: list[Engine] = []
        seen: set[str] = set()
        pool = get_failover_pool()
        cap = max(1, min(8, int(limit)))
        for model_id in ordered_ids:
            if model_id in seen:
                continue
            seen.add(model_id)
            engine = await self.engine_for_id(model_id)
            if engine is None or not engine.live:
                continue
            if not is_pinned_live_route(
                engine.provider, engine.litellm_model or engine.model_string or engine.label
            ):
                continue
            if pool.is_quarantined(engine.model_string or engine.label):
                continue
            engines.append(engine)
            if len(engines) >= cap:
                return engines
        for extra in pool.engines_for(kind, limit=cap):
            key = extra.model_string or extra.label
            if key in seen or extra.label in seen:
                continue
            seen.add(key)
            seen.add(extra.label)
            engines.append(extra)
            if len(engines) >= cap:
                break
        return engines[:cap]

    async def engine_for_id(self, model_id: str) -> Engine | None:
        row = await self.db.get_model_row(model_id)
        if row is None or not int(row.get("is_active") or 0):
            return None
        return await self._row_to_engine(row)

    async def bootstrap(self) -> None:
        await self.db.init_schema()
        await self.ensure_free_routes()
        await self.refresh_cache()
        for row in self.models_cache:
            await self._sync_route(str(row["id"]))
        from robber.router import set_hybrid_from_labels

        set_hybrid_from_labels(
            [
                "OpenRouter Nemotron 3.5 Lightning",
                "Groq Llama 3.3",
                "Gemini 2.5 Flash",
            ]
        )
        pool = get_failover_pool()
        pool.set_key_resolver(resolve_endpoint_secret)
        pool.hydrate_from_cache()

    async def ensure_free_routes(self) -> None:
        """Register BYOK free/cheap routes. Secrets stay in env, never SQLite."""
        for row in FREE_ROUTE_SEEDS:
            await self.db.add_model(dict(row))

    async def discover_from_row(
        self,
        model_id: str,
        *,
        api_key: str = "",
        force: bool = False,
    ) -> DiscoveryResult | None:
        row = await self.db.get_model_row(model_id)
        if row is None:
            return None
        public = self.db._public_model(row)
        base = str(public.get("base_url") or "").strip()
        protocol = str(public.get("protocol_type") or PROTOCOL_OPENAI)
        if not base or protocol != PROTOCOL_OPENAI:
            return None
        secret = api_key.strip() or await self._secret_for(row)
        preset = str(public.get("provider_preset") or "CUSTOM")
        headers = public.get("custom_headers") if isinstance(public.get("custom_headers"), dict) else {}
        hint = get_preset(preset).get("env_hint") or ""
        result = await discover_and_ingest(
            provider_id=preset.lower(),
            base_url=base,
            api_key=secret,
            extra_headers=dict(headers or {}),
            env_hint=str(hint),
            force=force,
        )
        self.last_discovery = result
        return result

    async def discover_known(self, *, force: bool = False) -> list[DiscoveryResult]:
        results: list[DiscoveryResult] = []
        seen: set[str] = set()
        for preset in PRESETS.values():
            if str(preset.get("protocol") or "") != PROTOCOL_OPENAI:
                continue
            base = str(preset.get("base_url") or "").strip().rstrip("/")
            if not base or base in seen:
                continue
            secret = resolve_endpoint_secret(str(preset["id"]), str(preset.get("env_hint") or ""))
            if not secret:
                continue
            seen.add(base)
            result = await discover_and_ingest(
                provider_id=str(preset["id"]).lower(),
                base_url=base,
                api_key=secret,
                extra_headers=dict(preset.get("headers") or {}),
                env_hint=str(preset.get("env_hint") or ""),
                force=force,
            )
            results.append(result)
        for row in self.models_cache:
            base = str(row.get("base_url") or "").strip().rstrip("/")
            protocol = str(row.get("protocol_type") or "")
            if not base or base in seen or protocol != PROTOCOL_OPENAI:
                continue
            raw = await self.db.get_model_row(str(row["id"]))
            if raw is None:
                continue
            secret = await self._secret_for(raw)
            if not secret:
                continue
            seen.add(base)
            result = await discover_and_ingest(
                provider_id=str(row.get("provider_preset") or "custom").lower(),
                base_url=base,
                api_key=secret,
                extra_headers=dict(row.get("custom_headers") or {}),
                env_hint=str(get_preset(str(row.get("provider_preset") or "CUSTOM")).get("env_hint") or ""),
                force=force,
            )
            results.append(result)
        if results:
            self.last_discovery = results[-1]
        return results

    async def _secret_for(self, row: dict[str, Any]) -> str:
        stored = await self.db.get_model_secret(str(row["id"]))
        if stored:
            return stored
        preset = get_preset(str(row.get("provider_preset") or "CUSTOM"))
        hint = preset.get("env_hint") or ""
        if hint in {"GEMINI_API_KEY", "GOOGLE_API_KEY"}:
            return get_key("gemini") or ""
        provider_map = {
            "ANTHROPIC_API_KEY": "anthropic",
            "OPENAI_API_KEY": "openai",
            "OPENROUTER_API_KEY": "openrouter",
            "GROQ_API_KEY": "groq",
            "MISTRAL_API_KEY": "mistral",
            "COHERE_API_KEY": "cohere",
            "DEEPSEEK_API_KEY": "deepseek",
            "OPENCODE_API_KEY": "opencode",
            "TOGETHER_API_KEY": "together",
            "LITELLM_MASTER_KEY": "litellm",
            "NEWAPI_API_KEY": "new_api",
            "OMNIROUTE_API_KEY": "omniroute",
            "TOKENROUTER_API_KEY": "tokenrouter",
        }
        mapped = provider_map.get(hint)
        if mapped:
            return get_key(mapped) or ""
        preset_id = str(row.get("provider_preset") or "").strip().lower()
        return get_key(preset_id) or resolve_endpoint_secret(preset_id, str(hint)) or ""

    async def _row_to_engine(self, row: dict[str, Any]) -> Engine:
        public = self.db._public_model(row)
        secret = await self._secret_for(row)
        name = public["name"]
        protocol = public["protocol_type"]
        model_string = public["model_string"] or name
        base = public["base_url"]
        headers = public["custom_headers"] or None
        fallback = public["fallback_model_id"]
        fallback_label = None
        if fallback:
            other = await self.db.get_model_row(str(fallback))
            if other:
                fallback_label = str(other.get("name") or fallback)
        preset = public["provider_preset"]
        provider = preset.lower()
        if protocol == PROTOCOL_NATIVE and preset == "GEMINI":
            litellm = model_string if "/" in model_string else f"gemini/{model_string}"
            return Engine(
                name,
                litellm,
                "gemini",
                secret or None,
                None,
                headers,
                fallback_label,
                self.active_batch,
                protocol,
                model_string,
            )
        if protocol == PROTOCOL_NATIVE and preset == "OLLAMA":
            litellm = model_string if "/" in model_string else f"ollama/{model_string}"
            return Engine(
                name,
                litellm,
                "ollama",
                None,
                base or "http://127.0.0.1:11434",
                headers,
                fallback_label,
                "eco",
                protocol,
                model_string,
            )
        if protocol == PROTOCOL_ANTHROPIC:
            litellm = model_string if "/" in model_string else f"anthropic/{model_string}"
            return Engine(
                name,
                litellm,
                "anthropic",
                secret or None,
                base or None,
                headers,
                fallback_label,
                self.active_batch,
                protocol,
                model_string,
            )
        http_model, groq_litellm = completion_model_ids(
            model_string=model_string,
            litellm_model=None,
            api_base=base,
        )
        if groq_litellm.startswith("groq/") or preset == "GROQ":
            return Engine(
                name,
                groq_litellm if groq_litellm.startswith("groq/") else f"groq/{http_model}",
                "groq",
                secret or None,
                base or None,
                headers,
                fallback_label,
                self.active_batch,
                protocol,
                http_model or model_string,
            )
        if preset == "OPENROUTER":
            slug = model_string
            if not slug.startswith("openrouter/"):
                slug = f"openrouter/{slug}"
            return Engine(
                name,
                slug,
                "openrouter",
                secret or None,
                None,
                headers,
                fallback_label,
                self.active_batch,
                protocol,
                model_string,
            )
        if preset in {"OMNIROUTE", "TOKEN_ROUTER"}:
            provider = "omniroute" if preset == "OMNIROUTE" else "tokenrouter"
            litellm = (
                model_string if model_string.startswith("openai/") else f"openai/{model_string}"
            )
            return Engine(
                name,
                litellm,
                provider,
                secret or None,
                base or str(get_preset(preset).get("base_url") or "") or None,
                headers,
                fallback_label,
                self.active_batch,
                PROTOCOL_OPENAI,
                model_string,
            )
        litellm = model_string if "/" in model_string else f"openai/{model_string}"
        return Engine(
            name,
            litellm,
            "openai",
            secret or ("sk-local" if preset == "OLLAMA" else None),
            base or None,
            headers,
            fallback_label,
            self.active_batch,
            protocol,
            model_string,
        )

    async def _pool_for(self, combo: dict[str, Any], kind: TaskKind) -> list[str]:
        reasoning = str(combo.get("reasoning_model_id") or "").strip()
        coding = [str(item) for item in combo.get("coding_model_ids") or [] if str(item).strip()]
        general = [str(item) for item in combo.get("general_model_ids") or [] if str(item).strip()]
        if kind == "reasoning":
            pool = [reasoning] if reasoning else general or coding
        elif kind == "coding":
            pool = coding or general
        else:
            pool = general or coding
        pool = [item for item in pool if item]
        if not pool:
            return []
        if kind == "reasoning":
            rest = [item for item in coding + general if item and item != pool[0]]
            return pool + rest
        key = f"{combo.get('id')}:{kind}"
        async with self._lock:
            start = self._rr.get(key, 0) % len(pool)
            self._rr[key] = start + 1
        rotated = pool[start:] + pool[:start]
        extras: list[str] = []
        if kind == "coding":
            extras = [item for item in general if item not in rotated]
        elif kind == "general":
            extras = [item for item in coding if item not in rotated]
        if reasoning and reasoning not in rotated:
            extras.append(reasoning)
        return rotated + extras

    async def _sync_route(self, model_id: str) -> None:
        row = await self.db.get_model_row(model_id)
        if row is None:
            return
        engine = await self._row_to_engine(row)
        if engine.litellm_model is None:
            return
        register_route(
            engine.label,
            engine.provider,
            engine.litellm_model,
            id=str(row["id"]),
            provider_type="custom_base" if engine.protocol_type != PROTOCOL_NATIVE else "native",
            api_base=engine.api_base or "",
            custom_headers=engine.extra_headers or {},
            fallback_label=engine.fallback_label or "",
            fallback_model_id=str(row.get("fallback_model_id") or ""),
            active=bool(int(row.get("is_active") or 0)),
            context_length=int(row.get("context_length") or 128000),
            cost_per_m=float(row.get("cost_per_1m") or 0.0),
            latency_ms=int(row.get("latency_ms") or 0),
            builtin=False,
        )


# Keep the old name used by server.py
ModelManager = CustomModelManager
