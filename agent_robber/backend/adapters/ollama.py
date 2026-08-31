"""Ollama local adapter.

Ollama exposes an OpenAI-compatible /v1/chat/completions endpoint but
needs no API key. Ping hits /api/tags first to verify the daemon is up,
then optionally pings the completions endpoint.
"""

from __future__ import annotations

import time

import httpx

from agent_robber.backend.adapters.base import BaseAdapter, PingResult


class OllamaAdapter(BaseAdapter):
    provider_id = "ollama"

    async def ping(
        self,
        base_url: str,
        api_key: str,
        model_string: str,
    ) -> PingResult:
        base = base_url.rstrip("/")
        # Derive the tags endpoint from whatever base URL was provided
        tags_url = base
        if "/v1" in tags_url:
            tags_url = tags_url.replace("/v1", "")
        tags_url = tags_url.rstrip("/") + "/api/tags"

        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(tags_url)
            elapsed = int((time.monotonic() - t0) * 1000)

            if resp.status_code == 200:
                return PingResult(True, elapsed, resp.status_code, f"Ollama daemon OK ({elapsed}ms)")
            return PingResult(False, elapsed, resp.status_code, f"HTTP {resp.status_code}")

        except httpx.ConnectError:
            elapsed = int((time.monotonic() - t0) * 1000)
            return PingResult(False, elapsed, 0, "Ollama daemon not reachable")
        except httpx.TimeoutException:
            elapsed = int((time.monotonic() - t0) * 1000)
            return PingResult(False, elapsed, 0, "Timeout")
        except Exception as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return PingResult(False, elapsed, 0, str(exc))
