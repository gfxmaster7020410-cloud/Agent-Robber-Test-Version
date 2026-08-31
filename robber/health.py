"""Endpoint health probes used before routing a live completion."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from robber.router import Engine

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def normalize_http_url(raw: str, default: str = "http://127.0.0.1:11434") -> str:
    text = (raw or default).strip() or default
    if "://" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("URL must be http(s) with a host")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def ollama_host_allowed(raw: str) -> str:
    """Only local Ollama hosts — no SSRF to the public internet."""
    host = normalize_http_url(raw, "http://127.0.0.1:11434")
    parsed = urlparse(host)
    hostname = (parsed.hostname or "").lower()
    if hostname not in LOCAL_HOSTS:
        raise ValueError("Ollama host must be localhost / 127.0.0.1")
    return f"{parsed.scheme}://{parsed.netloc}"


async def _get_ok(url: str, headers: dict[str, str] | None = None) -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0, follow_redirects=False) as client:
            response = await client.get(url, headers=headers or {})
            return response.status_code < 500
    except Exception:
        return False


async def probe_engine(engine: Engine) -> bool:
    """Return True if this engine looks reachable enough to try a completion."""
    if not engine.live or engine.litellm_model is None:
        return False
    if engine.provider == "ollama":
        try:
            host = ollama_host_allowed(engine.api_base or "http://127.0.0.1:11434")
        except ValueError:
            return False
        return await _get_ok(f"{host}/api/tags")
    if engine.api_base:
        try:
            base = normalize_http_url(engine.api_base)
        except ValueError:
            return False
        headers = dict(engine.extra_headers or {})
        if engine.api_key and engine.api_key != "sk-local":
            headers.setdefault("Authorization", f"Bearer {engine.api_key}")
        if await _get_ok(f"{base}/models", headers):
            return True
        return await _get_ok(base, headers)
    return True


async def scan_ollama_tags(host: str) -> list[str]:
    base = ollama_host_allowed(host)
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        response = await client.get(f"{base}/api/tags")
        response.raise_for_status()
        payload = response.json()
    models = payload.get("models") if isinstance(payload, dict) else None
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
