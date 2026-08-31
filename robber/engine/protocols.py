"""OpenAI-compatible and Anthropic-compatible HTTP adapters.

Native Gemini / Ollama stay on the LiteLLM path in robber.router.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

PROTOCOL_OPENAI = "OPENAI_COMPATIBLE"
PROTOCOL_ANTHROPIC = "ANTHROPIC_COMPATIBLE"
PROTOCOL_NATIVE = "NATIVE"
ANTHROPIC_VERSION = "2023-06-01"


def _normalize_base(base: str) -> str:
    text = (base or "").strip().rstrip("/")
    if not text:
        raise ValueError("base_url is required for this protocol")
    if "://" not in text:
        text = f"https://{text}"
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be http(s)")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def openai_url(base_url: str) -> str:
    root = _normalize_base(base_url)
    if root.endswith("/chat/completions"):
        return root
    return f"{root}/chat/completions"


def anthropic_url(base_url: str) -> str:
    root = _normalize_base(base_url)
    if root.endswith("/messages"):
        return root
    return f"{root}/v1/messages"


def merge_headers(
    protocol: str,
    api_key: str,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if extra:
        for key, value in extra.items():
            name = str(key).strip()
            if name:
                headers[name] = str(value)
    token = (api_key or "").strip()
    if protocol == PROTOCOL_ANTHROPIC:
        headers.setdefault("anthropic-version", ANTHROPIC_VERSION)
        if token:
            headers.setdefault("x-api-key", token)
            headers.setdefault("Authorization", f"Bearer {token}")
    elif token:
        headers.setdefault("Authorization", f"Bearer {token}")
    return headers


def openai_body(
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    stream: bool = True,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": stream,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return payload


def anthropic_body(
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    stream: bool = True,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    system_parts: list[str] = []
    body_messages: list[dict[str, str]] = []
    for item in messages:
        role = str(item.get("role") or "user")
        content = str(item.get("content") or "")
        if role == "system":
            if content:
                system_parts.append(content)
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        body_messages.append({"role": role, "content": content})
    if not body_messages:
        body_messages = [{"role": "user", "content": ""}]
    payload: dict[str, Any] = {
        "model": model,
        "messages": body_messages,
        "max_tokens": max(1, int(max_tokens)),
        "temperature": temperature,
        "stream": stream,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    return payload


def _openai_delta(payload: dict[str, Any]) -> tuple[str, str]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", ""
    first = choices[0] if isinstance(choices[0], dict) else {}
    delta = first.get("delta") if isinstance(first, dict) else None
    if not isinstance(delta, dict):
        message = first.get("message") if isinstance(first, dict) else None
        if isinstance(message, dict):
            return "", str(message.get("content") or "")
        return "", ""
    reasoning = str(delta.get("reasoning_content") or delta.get("reasoning") or "")
    content = str(delta.get("content") or "")
    return reasoning, content


def _anthropic_delta(payload: dict[str, Any]) -> str:
    delta = payload.get("delta")
    if isinstance(delta, dict):
        text = delta.get("text")
        if isinstance(text, str):
            return text
    return ""


async def stream_openai(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    extra_headers: Mapping[str, str] | None = None,
    temperature: float = 0.2,
    timeout: float = 45.0,
) -> AsyncIterator[tuple[str, str]]:
    url = openai_url(base_url)
    headers = merge_headers(PROTOCOL_OPENAI, api_key, extra_headers)
    body = openai_body(model, messages, temperature=temperature, stream=True)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                text = line.strip()
                if not text or not text.startswith("data:"):
                    continue
                data = text[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                reasoning, content = _openai_delta(payload)
                if reasoning:
                    yield "reasoning", reasoning
                if content:
                    yield "assistant", content


async def stream_anthropic(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    extra_headers: Mapping[str, str] | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout: float = 45.0,
) -> AsyncIterator[tuple[str, str]]:
    url = anthropic_url(base_url)
    headers = merge_headers(PROTOCOL_ANTHROPIC, api_key, extra_headers)
    body = anthropic_body(model, messages, temperature=temperature, stream=True, max_tokens=max_tokens)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                text = line.strip()
                if not text.startswith("data:"):
                    continue
                data = text[5:].strip()
                if not data:
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                chunk = _anthropic_delta(payload)
                if chunk:
                    yield "assistant", chunk


async def ping_endpoint(
    *,
    protocol: str,
    base_url: str,
    api_key: str,
    extra_headers: Mapping[str, str] | None = None,
    timeout: float = 8.0,
) -> tuple[bool, int, str]:
    """Return (ok, latency_ms, error). Never includes the API key in error text."""
    t0 = time.monotonic()
    try:
        if protocol == PROTOCOL_ANTHROPIC:
            url = anthropic_url(base_url)
        elif base_url.strip():
            url = openai_url(base_url)
            # Prefer GET /models on the parent of chat/completions when possible.
            if url.endswith("/chat/completions"):
                url = url[: -len("/chat/completions")] + "/models"
        else:
            return True, 0, ""
        headers = merge_headers(protocol, api_key, extra_headers)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            if protocol == PROTOCOL_ANTHROPIC:
                response = await client.post(
                    url,
                    headers=headers,
                    json=anthropic_body("ping", [{"role": "user", "content": "ping"}], stream=False, max_tokens=8),
                )
            else:
                response = await client.get(url, headers=headers)
        elapsed = int((time.monotonic() - t0) * 1000)
        if response.status_code < 500:
            return True, elapsed, ""
        return False, elapsed, f"HTTP {response.status_code}"
    except httpx.HTTPStatusError as exc:
        elapsed = int((time.monotonic() - t0) * 1000)
        return False, elapsed, f"HTTP {exc.response.status_code}"
    except Exception as exc:
        elapsed = int((time.monotonic() - t0) * 1000)
        return False, elapsed, type(exc).__name__
