"""OpenAI-compatible provider adapter.

Works for OpenAI, DeepSeek, Groq, and any service exposing
POST /v1/chat/completions with bearer-token auth.
"""

from __future__ import annotations

import time

import httpx

from agent_robber.backend.adapters.base import BaseAdapter, PingResult


class OpenAIAdapter(BaseAdapter):
    provider_id = "openai"

    async def ping(
        self,
        base_url: str,
        api_key: str,
        model_string: str,
    ) -> PingResult:
        url = base_url.rstrip("/")
        if "/chat/completions" not in url:
            if url.endswith("/v1"):
                url += "/chat/completions"
            else:
                url += "/v1/chat/completions"

        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model_string,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
            "temperature": 0.0,
        }

        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
            elapsed = int((time.monotonic() - t0) * 1000)

            if resp.status_code < 400:
                return PingResult(True, elapsed, resp.status_code, f"OK ({elapsed}ms)")
            return PingResult(False, elapsed, resp.status_code, f"HTTP {resp.status_code}")

        except httpx.TimeoutException:
            elapsed = int((time.monotonic() - t0) * 1000)
            return PingResult(False, elapsed, 0, "Timeout")
        except httpx.ConnectError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return PingResult(False, elapsed, 0, f"ConnectError: {exc}")
        except Exception as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return PingResult(False, elapsed, 0, str(exc))
