"""Unified async generate() across OpenAI, Anthropic, Google AI, and local LLMs."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from robber.engine.protocols import anthropic_url, merge_headers, openai_url
from robber.vault import LOCAL_PROVIDERS

KIND_OPENAI = "openai"
KIND_ANTHROPIC = "anthropic"
KIND_GEMINI = "gemini"
KIND_OLLAMA = "ollama"

logger = logging.getLogger(__name__)

_PROVIDER_KIND = {
    "openai": KIND_OPENAI,
    "new_api": KIND_OPENAI,
    "groq": KIND_OPENAI,
    "openrouter": KIND_OPENAI,
    "omniroute": KIND_OPENAI,
    "tokenrouter": KIND_OPENAI,
    "token_router": KIND_OPENAI,
    "together": KIND_OPENAI,
    "deepseek": KIND_OPENAI,
    "mistral": KIND_OPENAI,
    "vllm": KIND_OPENAI,
    "lmstudio": KIND_OPENAI,
    "llamacpp": KIND_OPENAI,
    "llama.cpp": KIND_OPENAI,
    "gguf": KIND_OPENAI,
    "anthropic": KIND_ANTHROPIC,
    "gemini": KIND_GEMINI,
    "google": KIND_GEMINI,
    "ollama": KIND_OLLAMA,
}


class ProviderError(Exception):
    """Normalized provider failure. status_code is read by orchestrator.http_status_of."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        provider: str = "",
        kind: str = "error",
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code or 0)
        self.provider = provider
        self.kind = kind


@dataclass
class ModelRequest:
    prompt: str
    model: str
    provider: str
    api_key: str | None = None
    api_base: str | None = None
    messages: list[dict[str, str]] | None = None
    max_tokens: int | None = None
    temperature: float = 0.2
    protocol: str = ""

    def chat(self) -> list[dict[str, str]]:
        if self.messages:
            return list(self.messages)
        return [{"role": "user", "content": self.prompt or ""}]


@dataclass
class ModelResponse:
    text: str
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0
    finish_reason: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("raw", None)
        row["text"] = (self.text or "")[:400]
        return row


class UnifiedProviderProxy:
    """One async generate() surface; tests inject a client instead of hitting the network."""

    def __init__(self, *, timeout: float = 45.0, client: Any | None = None) -> None:
        self.timeout = max(1.0, float(timeout))
        self._client = client

    async def generate(self, request: ModelRequest) -> ModelResponse:
        kind = self.kind_for(request.provider, request.api_base, request.protocol)
        if not (request.model or "").strip():
            raise ProviderError(
                "model id is empty",
                status_code=400,
                provider=request.provider,
                kind="error",
            )
        needs_key = kind != KIND_OLLAMA and (request.provider or "").strip().lower() not in LOCAL_PROVIDERS
        if needs_key and not (request.api_key or "").strip():
            raise ProviderError(
                "API key missing",
                status_code=401,
                provider=request.provider,
                kind="auth",
            )
        started = time.perf_counter()
        if kind == KIND_ANTHROPIC:
            payload = await self._anthropic(request)
        elif kind == KIND_GEMINI:
            payload = await self._gemini(request)
        elif kind == KIND_OLLAMA:
            payload = await self._ollama(request)
        else:
            payload = await self._openai(request)
        payload.latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        payload.provider = request.provider
        payload.model = payload.model or request.model
        if payload.total_tokens <= 0:
            payload.total_tokens = payload.prompt_tokens + payload.completion_tokens
        return payload

    @staticmethod
    def kind_for(provider: str, api_base: str | None = None, protocol: str = "") -> str:
        proto = (protocol or "").upper()
        if proto == "ANTHROPIC_COMPATIBLE":
            return KIND_ANTHROPIC
        if proto == "OPENAI_COMPATIBLE":
            name = (provider or "").strip().lower()
            host = (api_base or "").rstrip("/").lower()
            if name in {"ollama", "local"}:
                if host.endswith("/v1"):
                    return KIND_OPENAI
                return KIND_OLLAMA
            if "11434" in host or "ollama" in host:
                return KIND_OLLAMA
            return KIND_OPENAI
        name = (provider or "").strip().lower()
        if name in {"ollama", "local"}:
            return KIND_OLLAMA
        if name in _PROVIDER_KIND:
            return _PROVIDER_KIND[name]
        host = (api_base or "").lower()
        if "anthropic" in host:
            return KIND_ANTHROPIC
        if "generativelanguage.googleapis.com" in host or "googleapis" in host:
            return KIND_GEMINI
        if "11434" in host or "ollama" in host:
            return KIND_OLLAMA
        return KIND_OPENAI

    async def _openai(self, request: ModelRequest) -> ModelResponse:
        base = request.api_base or _default_base(request.provider)
        url = openai_url(base)
        body = {
            "model": _bare_model(request.model),
            "messages": request.chat(),
            "temperature": request.temperature,
            "stream": False,
        }
        if request.max_tokens:
            body["max_tokens"] = int(request.max_tokens)
        data = await self._post(
            url,
            json=body,
            headers=merge_headers("OPENAI_COMPATIBLE", request.api_key or ""),
            provider=request.provider,
        )
        choice = ((data.get("choices") or [{}])[0]) if isinstance(data, dict) else {}
        message = choice.get("message") if isinstance(choice, dict) else {}
        usage = data.get("usage") if isinstance(data, dict) else {}
        text = ""
        if isinstance(message, dict):
            text = str(message.get("content") or "")
        return ModelResponse(
            text=text,
            model=str(data.get("model") or request.model),
            provider=request.provider,
            prompt_tokens=int((usage or {}).get("prompt_tokens") or 0),
            completion_tokens=int((usage or {}).get("completion_tokens") or 0),
            total_tokens=int((usage or {}).get("total_tokens") or 0),
            finish_reason=str(choice.get("finish_reason") or "stop"),
            raw=data if isinstance(data, dict) else {},
        )

    async def _anthropic(self, request: ModelRequest) -> ModelResponse:
        base = request.api_base or "https://api.anthropic.com"
        url = anthropic_url(base)
        system = ""
        messages: list[dict[str, str]] = []
        for item in request.chat():
            role = str(item.get("role") or "user")
            content = str(item.get("content") or "")
            if role == "system":
                system = content
            else:
                messages.append({"role": "user" if role != "assistant" else "assistant", "content": content})
        if not messages:
            messages = [{"role": "user", "content": request.prompt or ""}]
        body: dict[str, Any] = {
            "model": _bare_model(request.model),
            "messages": messages,
            "max_tokens": int(request.max_tokens or 1024),
            "temperature": request.temperature,
        }
        if system:
            body["system"] = system
        data = await self._post(
            url,
            json=body,
            headers=merge_headers("ANTHROPIC_COMPATIBLE", request.api_key or ""),
            provider=request.provider,
        )
        chunks = data.get("content") if isinstance(data, dict) else []
        text = "".join(str(part.get("text") or "") for part in chunks if isinstance(part, dict))
        usage = data.get("usage") if isinstance(data, dict) else {}
        return ModelResponse(
            text=text,
            model=str((data or {}).get("model") or request.model),
            provider=request.provider,
            prompt_tokens=int((usage or {}).get("input_tokens") or 0),
            completion_tokens=int((usage or {}).get("output_tokens") or 0),
            finish_reason=str((data or {}).get("stop_reason") or "stop"),
            raw=data if isinstance(data, dict) else {},
        )

    async def _gemini(self, request: ModelRequest) -> ModelResponse:
        model = _bare_model(request.model).replace("gemini/", "")
        base = (request.api_base or "https://generativelanguage.googleapis.com").rstrip("/")
        url = f"{base}/v1beta/models/{model}:generateContent"
        params = {}
        key = (request.api_key or "").strip()
        headers = {"Content-Type": "application/json"}
        if key:
            headers["x-goog-api-key"] = key
        parts = [{"text": str(item.get("content") or "")} for item in request.chat()]
        body = {"contents": [{"role": "user", "parts": parts or [{"text": request.prompt or ""}]}]}
        data = await self._post(url, json=body, headers=headers, params=params, provider=request.provider)
        candidates = data.get("candidates") if isinstance(data, dict) else []
        first = candidates[0] if candidates else {}
        content = first.get("content") if isinstance(first, dict) else {}
        inner = content.get("parts") if isinstance(content, dict) else []
        text = "".join(str(part.get("text") or "") for part in inner if isinstance(part, dict))
        usage = data.get("usageMetadata") if isinstance(data, dict) else {}
        return ModelResponse(
            text=text,
            model=model,
            provider=request.provider,
            prompt_tokens=int((usage or {}).get("promptTokenCount") or 0),
            completion_tokens=int((usage or {}).get("candidatesTokenCount") or 0),
            total_tokens=int((usage or {}).get("totalTokenCount") or 0),
            finish_reason=str(first.get("finishReason") or "stop").lower(),
            raw=data if isinstance(data, dict) else {},
        )

    async def _ollama(self, request: ModelRequest) -> ModelResponse:
        base = (request.api_base or "http://127.0.0.1:11434").rstrip("/")
        if base.endswith("/v1"):
            request = ModelRequest(
                prompt=request.prompt,
                model=request.model,
                provider=request.provider,
                api_key=request.api_key or "sk-local",
                api_base=base,
                messages=request.messages,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                protocol="OPENAI_COMPATIBLE",
            )
            return await self._openai(request)
        url = f"{base}/api/chat"
        body = {
            "model": _bare_model(request.model),
            "messages": request.chat(),
            "stream": False,
        }
        data = await self._post(url, json=body, headers={"Content-Type": "application/json"}, provider=request.provider)
        message = data.get("message") if isinstance(data, dict) else {}
        return ModelResponse(
            text=str((message or {}).get("content") or ""),
            model=str((data or {}).get("model") or request.model),
            provider=request.provider,
            prompt_tokens=int((data or {}).get("prompt_eval_count") or 0),
            completion_tokens=int((data or {}).get("eval_count") or 0),
            finish_reason=str((data or {}).get("done_reason") or "stop"),
            raw=data if isinstance(data, dict) else {},
        )

    async def _post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        provider: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        client = self._client
        own = False
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout)
            own = True
        try:
            response = await client.post(url, json=json, headers=headers, params=params or {})
        except httpx.HTTPError as exc:
            raise ProviderError(f"{type(exc).__name__}: {exc}", status_code=0, provider=provider, kind="network") from exc
        finally:
            if own:
                await client.aclose()
        status = int(getattr(response, "status_code", 0) or 0)
        try:
            data = response.json()
        except Exception:
            data = {"error": (getattr(response, "text", "") or "")[:400]}
        if status >= 400:
            message = _error_message(data, status)
            logger.error(
                "[ROUTER ERROR] Provider %s failed with status %s: %s",
                provider,
                status,
                message,
            )
            raise ProviderError(
                message,
                status_code=status,
                provider=provider,
                kind=_kind_from_status(status, data),
            )
        return data if isinstance(data, dict) else {"value": data}


def _bare_model(model: str) -> str:
    text = (model or "").strip()
    if "/" in text and not text.startswith("http"):
        prefix, _, rest = text.partition("/")
        if prefix in {"openai", "anthropic", "gemini", "groq", "ollama", "openrouter"}:
            return rest or text
    return text


def _default_base(provider: str) -> str:
    name = (provider or "").lower()
    if name == "groq":
        return "https://api.groq.com/openai/v1"
    if name == "openai":
        return "https://api.openai.com/v1"
    if name == "openrouter":
        return "https://openrouter.ai/api/v1"
    if name == "omniroute":
        return "https://api.omniroute.ai/v1"
    if name in {"tokenrouter", "token_router"}:
        return "https://api.tokenrouter.io/v1"
    return "https://api.openai.com/v1"


def _error_message(data: Any, status: int) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return f"HTTP {status}: {err.get('message')}"
        if isinstance(err, str) and err.strip():
            return f"HTTP {status}: {err}"
        if data.get("message"):
            return f"HTTP {status}: {data.get('message')}"
    return f"HTTP {status}"


def _kind_from_status(status: int, data: Any) -> str:
    blob = str(data).lower()
    if status == 429 or "rate limit" in blob:
        return "rate_limit"
    if status in {500, 502, 503, 504}:
        return "server"
    if "context" in blob or "maximum" in blob and "token" in blob:
        return "overflow"
    if status in {400, 413}:
        return "overflow" if "token" in blob or "context" in blob else "error"
    return "error"


def safe_host(url: str) -> str:
    try:
        parsed = urlparse(url)
        return parsed.netloc or url
    except Exception:
        return ""
