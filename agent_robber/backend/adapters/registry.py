"""Provider adapter registry.

Maps provider IDs to their adapter implementations and provides
a single lookup for ping dispatch.
"""

from __future__ import annotations

from agent_robber.backend.adapters.base import BaseAdapter, PingResult
from agent_robber.backend.adapters.openai import OpenAIAdapter
from agent_robber.backend.adapters.ollama import OllamaAdapter

# Registry: provider_id -> adapter instance
_REGISTRY: dict[str, BaseAdapter] = {
    "openai": OpenAIAdapter(),
    "anthropic": OpenAIAdapter(),   # Anthropic also exposes OpenAI-compat via /v1
    "deepseek": OpenAIAdapter(),
    "groq": OpenAIAdapter(),
    "ollama": OllamaAdapter(),
}

_DEFAULT = OpenAIAdapter()


def get_adapter(provider_id: str) -> BaseAdapter:
    return _REGISTRY.get(provider_id, _DEFAULT)


async def ping_provider(
    provider_id: str,
    base_url: str,
    api_key: str,
    model_string: str,
) -> PingResult:
    adapter = get_adapter(provider_id)
    return await adapter.ping(base_url, api_key, model_string)
