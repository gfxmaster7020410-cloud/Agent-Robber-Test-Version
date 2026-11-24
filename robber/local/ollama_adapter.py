"""Local Ollama / LM Studio / vLLM / llama.cpp (GGUF) offload for 8GB RAM."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from robber.config import (
    LOCAL_CONTEXT_WINDOW,
    LOCAL_LLAMACPP_HOST,
    LOCAL_LMSTUDIO_HOST,
    LOCAL_OLLAMA_HOST,
    LOCAL_PROBE_TIMEOUT,
    LOCAL_VLLM_HOST,
    PREFERRED_LOCAL_MODELS,
)
from robber.health import LOCAL_HOSTS, normalize_http_url
from robber.router.model_router import RouteDecision, RouteTarget

ProbeFn = Callable[[str], dict[str, Any] | None]


@dataclass
class LocalEndpoint:
    kind: str
    host: str
    models: list[str] = field(default_factory=list)
    ok: bool = False

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, "host": self.host, "ok": self.ok, "models": list(self.models)}


class OllamaAdapter:
    """Detect localhost inference servers and register them as zero-cost fallbacks."""

    def __init__(
        self,
        *,
        hosts: dict[str, str] | None = None,
        probe: ProbeFn | None = None,
        timeout: float = LOCAL_PROBE_TIMEOUT,
        context_window: int = LOCAL_CONTEXT_WINDOW,
    ) -> None:
        self.hosts = hosts or {
            "ollama": (os.environ.get("OLLAMA_API_BASE") or os.environ.get("OLLAMA_HOST") or LOCAL_OLLAMA_HOST).strip(),
            "lmstudio": (os.environ.get("LMSTUDIO_API_BASE") or LOCAL_LMSTUDIO_HOST).strip(),
            "vllm": (os.environ.get("VLLM_API_BASE") or LOCAL_VLLM_HOST).strip(),
            "llamacpp": (
                os.environ.get("LLAMACPP_API_BASE") or os.environ.get("LLAMA_CPP_HOST") or LOCAL_LLAMACPP_HOST
            ).strip(),
        }
        self._probe = probe or _http_json
        self.timeout = max(0.2, float(timeout))
        self.context_window = max(1024, min(int(context_window), LOCAL_CONTEXT_WINDOW))
        self.endpoints: list[LocalEndpoint] = []
        self.preferred: str = ""
        self.gguf_files: list[str] = []
        self._targets: list[RouteTarget] = []

    def scan(self) -> list[LocalEndpoint]:
        found: list[LocalEndpoint] = []
        for kind, host in self.hosts.items():
            try:
                base = _local_only(host)
            except ValueError:
                continue
            names = self._tags(kind, base)
            row = LocalEndpoint(kind=kind, host=base, models=names, ok=bool(names))
            found.append(row)
        extra = _gguf_names()
        self.gguf_files = extra
        if extra:
            attached = False
            for row in found:
                if row.kind == "llamacpp":
                    for name in extra:
                        if name not in row.models:
                            row.models.append(name)
                    attached = True
            if not attached:
                try:
                    host = _local_only(self.hosts.get("llamacpp") or LOCAL_LLAMACPP_HOST)
                except ValueError:
                    host = LOCAL_LLAMACPP_HOST
                found.append(LocalEndpoint(kind="llamacpp", host=host, models=list(extra), ok=False))
        self.endpoints = found
        self.preferred = _pick_preferred(self.all_models())
        self._targets = self._build_targets()
        if self._targets:
            self.register()
        return found

    def ingest(self, kind: str, host: str, models: list[str], *, register: bool = False) -> None:
        base = _local_only(host)
        names = [item.strip() for item in models if str(item).strip()]
        self.endpoints = [item for item in self.endpoints if item.kind != kind]
        self.endpoints.append(LocalEndpoint(kind=kind, host=base, models=names, ok=bool(names)))
        self.preferred = _pick_preferred(self.all_models())
        self._targets = self._build_targets()
        if register and self._targets:
            self.register()

    def all_models(self) -> list[str]:
        names: list[str] = []
        for row in self.endpoints:
            for name in row.models:
                if name not in names:
                    names.append(name)
        return names

    def throttle(self, prompt: str) -> tuple[str, int, bool]:
        from robber.memory.token_budgeter import TokenBudgeter

        cap = max(256, min(int(self.context_window), LOCAL_CONTEXT_WINDOW))
        return TokenBudgeter(cap, hard_cap=cap).fit(prompt or "")

    def fallback_targets(self) -> list[RouteTarget]:
        return list(self._targets)

    def register(self) -> list[str]:
        from robber.router.session import MODEL_ROUTES, merge_meta, register_route

        labels: list[str] = []
        for target in self._targets:
            register_route(
                target.label,
                target.provider,
                target.model,
                provider_type="local_ollama",
                api_base=target.api_base,
                context_length=target.context_length,
                cost_per_m=0.0,
                batch_class="eco",
                fallback_label="GPT-X / Local",
                active=True,
            )
            labels.append(target.label)
        preferred = next((item for item in self._targets if item.label == "GPT-X / Local"), None)
        if preferred is None and self._targets:
            preferred = self._targets[0]
        if preferred is not None:
            MODEL_ROUTES["GPT-X / Local"] = ((preferred.model, preferred.provider),)
            merge_meta(
                "GPT-X / Local",
                provider_type="local_ollama",
                api_base=preferred.api_base,
                context_length=self.context_window,
                cost_per_m=0.0,
                batch_class="eco",
            )
        return labels

    def public(self) -> dict[str, Any]:
        return {
            "window": self.context_window,
            "preferred": self.preferred,
            "endpoints": [item.public() for item in self.endpoints],
            "targets": [item.label for item in self._targets],
            "gguf": list(self.gguf_files),
        }

    def _tags(self, kind: str, base: str) -> list[str]:
        if kind == "ollama":
            payload = self._probe(f"{base}/api/tags")
            return _ollama_names(payload)
        payload = self._probe(f"{base}/v1/models")
        return _openai_names(payload)

    async def complete(self, prompt: str, *, model: str = "", api_base: str = "") -> str:
        """Non-streaming local completion. Never leaves localhost."""
        if not any(row.ok for row in self.endpoints):
            self.scan()
        packed, _, _ = self.throttle(prompt or "")
        host = (api_base or "").rstrip("/")
        provider = "ollama"
        picked = ""
        for row in self.endpoints:
            if not row.ok:
                continue
            if host and row.host.rstrip("/") != host:
                continue
            provider = "ollama" if row.kind == "ollama" else row.kind
            host = row.host.rstrip("/")
            picked = row.models[0] if row.models else ""
            break
        if not host:
            return ""
        name = (model or self.preferred or picked).strip()
        from robber.router.provider_proxy import ModelRequest, UnifiedProviderProxy

        proxy = UnifiedProviderProxy(timeout=12.0)
        response = await proxy.generate(
            ModelRequest(
                prompt=packed,
                model=name,
                provider=provider,
                api_base=host,
                protocol="NATIVE" if provider in {"ollama", "local"} else "OPENAI_COMPATIBLE",
                max_tokens=512,
            )
        )
        return str(response.text or "")

    def _build_targets(self) -> list[RouteTarget]:
        out: list[RouteTarget] = []
        seen: set[str] = set()
        ordered = _order_models(self.all_models())
        for name in ordered[:4]:
            endpoint = self._endpoint_for(name)
            if endpoint is None:
                continue
            provider = "ollama" if endpoint.kind == "ollama" else endpoint.kind
            protocol = "NATIVE" if provider == "ollama" else "OPENAI_COMPATIBLE"
            label = "GPT-X / Local" if not out else f"Local {name}"
            if label in seen:
                continue
            seen.add(label)
            model = name if "/" in name or provider != "ollama" else f"ollama/{name}"
            out.append(
                RouteTarget(
                    label=label,
                    provider=provider,
                    model=model,
                    api_base=endpoint.host,
                    context_length=self.context_window,
                    cost_per_m=0.0,
                    protocol=protocol,
                    batch_class="eco",
                )
            )
        return out

    def _endpoint_for(self, model: str) -> LocalEndpoint | None:
        for row in self.endpoints:
            if model in row.models and row.ok:
                return row
        return None


def attach_local_fallbacks(decision: RouteDecision, adapter: OllamaAdapter | None = None) -> RouteDecision:
    """Append cached zero-cost local targets without probing the network."""
    engine = adapter or get_adapter()
    extras = engine.fallback_targets()
    if not extras:
        return decision
    known = {item.label for item in decision.chain}
    extra = [item for item in extras if item.label not in known]
    if extra:
        packed, tokens, clipped = engine.throttle(decision.prompt)
        decision.prompt = packed
        decision.tokens = tokens
        decision.clipped = decision.clipped or clipped
        decision.fallbacks = [*decision.fallbacks, *extra]
    return decision


def _order_models(names: list[str]) -> list[str]:
    ranked: list[str] = []
    for needle in PREFERRED_LOCAL_MODELS:
        for name in names:
            if needle.split(":")[0] in name.lower() and name not in ranked:
                ranked.append(name)
    for name in names:
        if name not in ranked:
            ranked.append(name)
    return ranked


def _pick_preferred(names: list[str]) -> str:
    ordered = _order_models(names)
    return ordered[0] if ordered else ""


def _ollama_names(payload: dict[str, Any] | None) -> list[str]:
    if not isinstance(payload, dict):
        return []
    models = payload.get("models")
    names: list[str] = []
    if isinstance(models, list):
        for item in models:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("model") or "").strip()
            else:
                name = str(item).strip()
            if name and name not in names:
                names.append(name)
    return names


def _openai_names(payload: dict[str, Any] | None) -> list[str]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    names: list[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = str(item.get("id") or item.get("name") or "").strip()
            else:
                name = str(item).strip()
            if name and name not in names:
                names.append(name)
    return names


def _local_only(raw: str) -> str:
    host = normalize_http_url(raw, LOCAL_OLLAMA_HOST)
    parsed = urlparse(host)
    hostname = (parsed.hostname or "").lower()
    if hostname not in LOCAL_HOSTS:
        raise ValueError("local model host must be localhost")
    return f"{parsed.scheme}://{parsed.netloc}"


def _gguf_names(root: Path | str | None = None) -> list[str]:
    """List local *.gguf filenames only. Never mmap or read weight bytes."""
    from robber.jail import workspace_root

    base = Path(root).resolve() if root else workspace_root()
    names: list[str] = []
    for folder in (base / "models", base / "data" / "gguf"):
        if not folder.is_dir():
            continue
        for path in folder.glob("*.gguf"):
            name = path.name
            if name and name not in names:
                names.append(name)
    return names


def _http_json(url: str) -> dict[str, Any] | None:
    try:
        parsed = urlparse(url)
        if (parsed.hostname or "").lower() not in LOCAL_HOSTS:
            return None
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=LOCAL_PROBE_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        data = json.loads(body)
        return data if isinstance(data, dict) else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return None


_ADAPTER: OllamaAdapter | None = None


def get_adapter() -> OllamaAdapter:
    global _ADAPTER
    if _ADAPTER is None:
        _ADAPTER = OllamaAdapter()
    return _ADAPTER


def bind_adapter(adapter: OllamaAdapter | None) -> None:
    global _ADAPTER
    _ADAPTER = adapter
