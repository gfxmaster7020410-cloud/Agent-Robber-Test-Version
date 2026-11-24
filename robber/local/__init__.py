"""Local inference adapters."""

from robber.local.ollama_adapter import OllamaAdapter, attach_local_fallbacks, bind_adapter, get_adapter

__all__ = ["OllamaAdapter", "attach_local_fallbacks", "bind_adapter", "get_adapter"]
