"""Built-in LLM provider presets for the orchestration control panel.

Secrets never live here — only default base URLs, protocol, and header *names*.
"""

from __future__ import annotations

from typing import Any, TypedDict

ProtocolType = str  # OPENAI_COMPATIBLE | ANTHROPIC_COMPATIBLE | NATIVE


class Preset(TypedDict):
    id: str
    label: str
    protocol: str
    base_url: str
    model_placeholder: str
    headers: dict[str, str]
    env_hint: str


PRESETS: dict[str, Preset] = {
    "ANTHROPIC": {
        "id": "ANTHROPIC",
        "label": "Anthropic",
        "protocol": "ANTHROPIC_COMPATIBLE",
        "base_url": "https://api.anthropic.com",
        "model_placeholder": "claude-sonnet-4-20250514",
        "headers": {"anthropic-version": "2023-06-01"},
        "env_hint": "ANTHROPIC_API_KEY",
    },
    "OPENAI": {
        "id": "OPENAI",
        "label": "OpenAI",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "https://api.openai.com/v1",
        "model_placeholder": "gpt-4o-mini",
        "headers": {},
        "env_hint": "OPENAI_API_KEY",
    },
    "MISTRAL": {
        "id": "MISTRAL",
        "label": "Mistral",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "https://api.mistral.ai/v1",
        "model_placeholder": "mistral-small-latest",
        "headers": {},
        "env_hint": "MISTRAL_API_KEY",
    },
    "ZAI": {
        "id": "ZAI",
        "label": "Z.ai",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "https://api.z.ai/api/paas/v4",
        "model_placeholder": "glm-4.5-flash",
        "headers": {},
        "env_hint": "ZAI_API_KEY",
    },
    "LLAMA": {
        "id": "LLAMA",
        "label": "Llama (OpenAI-compat)",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "https://api.together.xyz/v1",
        "model_placeholder": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "headers": {},
        "env_hint": "TOGETHER_API_KEY",
    },
    "MIMO": {
        "id": "MIMO",
        "label": "Mimo",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "",
        "model_placeholder": "mimo-v2",
        "headers": {},
        "env_hint": "MIMO_API_KEY",
    },
    "DEEPSEEK": {
        "id": "DEEPSEEK",
        "label": "DeepSeek",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "https://api.deepseek.com/v1",
        "model_placeholder": "deepseek-chat",
        "headers": {},
        "env_hint": "DEEPSEEK_API_KEY",
    },
    "ALIBABA": {
        "id": "ALIBABA",
        "label": "Alibaba Qwen",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model_placeholder": "qwen-plus",
        "headers": {},
        "env_hint": "DASHSCOPE_API_KEY",
    },
    "OPENROUTER": {
        "id": "OPENROUTER",
        "label": "OpenRouter",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "https://openrouter.ai/api/v1",
        "model_placeholder": "deepseek/deepseek-r1",
        "headers": {"HTTP-Referer": "http://127.0.0.1:5173", "X-Title": "Agent Robber"},
        "env_hint": "OPENROUTER_API_KEY",
    },
    "TOKEN_ROUTER": {
        "id": "TOKEN_ROUTER",
        "label": "Token Router",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "https://api.tokenrouter.io/v1",
        "model_placeholder": "gpt-4o-mini",
        "headers": {},
        "env_hint": "TOKENROUTER_API_KEY",
    },
    "GEMINI": {
        "id": "GEMINI",
        "label": "Gemini (LiteLLM native)",
        "protocol": "NATIVE",
        "base_url": "",
        "model_placeholder": "gemini-3.6-flash",
        "headers": {},
        "env_hint": "GEMINI_API_KEY",
    },
    "GOOGLE_AI_STUDIO": {
        "id": "GOOGLE_AI_STUDIO",
        "label": "Google AI Studio",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model_placeholder": "gemini-3.6-flash",
        "headers": {},
        "env_hint": "GEMINI_API_KEY",
    },
    "OPENCODE_ZEN": {
        "id": "OPENCODE_ZEN",
        "label": "OpenCode Zen",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "https://opencode.ai/zen/v1",
        "model_placeholder": "opencode/zen",
        "headers": {},
        "env_hint": "OPENCODE_API_KEY",
    },
    "OPENCODE_GO": {
        "id": "OPENCODE_GO",
        "label": "OpenCode Go",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "https://opencode.ai/go/v1",
        "model_placeholder": "opencode/go",
        "headers": {},
        "env_hint": "OPENCODE_API_KEY",
    },
    "KILO": {
        "id": "KILO",
        "label": "Kilo",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "",
        "model_placeholder": "kilo-code",
        "headers": {},
        "env_hint": "KILO_API_KEY",
    },
    "VERTEX": {
        "id": "VERTEX",
        "label": "Google Vertex AI",
        "protocol": "NATIVE",
        "base_url": "",
        "model_placeholder": "gemini-3.6-flash",
        "headers": {},
        "env_hint": "GOOGLE_APPLICATION_CREDENTIALS",
    },
    "MINIMAX": {
        "id": "MINIMAX",
        "label": "MiniMax",
        "protocol": "ANTHROPIC_COMPATIBLE",
        "base_url": "https://api.minimax.io/anthropic",
        "model_placeholder": "MiniMax-M2.5",
        "headers": {"anthropic-version": "2023-06-01"},
        "env_hint": "MINIMAX_API_KEY",
    },
    "COHERE": {
        "id": "COHERE",
        "label": "Cohere",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "https://api.cohere.ai/compatibility/v1",
        "model_placeholder": "command-r",
        "headers": {},
        "env_hint": "COHERE_API_KEY",
    },
    "WATSONX": {
        "id": "WATSONX",
        "label": "IBM watsonx",
        "protocol": "NATIVE",
        "base_url": "",
        "model_placeholder": "ibm/granite-3-8b-instruct",
        "headers": {},
        "env_hint": "WATSONX_APIKEY",
    },
    "OMNIROUTE": {
        "id": "OMNIROUTE",
        "label": "OmniRoute",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "https://api.omniroute.ai/v1",
        "model_placeholder": "deepseek/deepseek-r1",
        "headers": {},
        "env_hint": "OMNIROUTE_API_KEY",
    },
    "NEW_API": {
        "id": "NEW_API",
        "label": "New API",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "http://127.0.0.1:3000/v1",
        "model_placeholder": "gpt-4o-mini",
        "headers": {},
        "env_hint": "NEWAPI_API_KEY",
    },
    "LITELLM": {
        "id": "LITELLM",
        "label": "LiteLLM proxy",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "http://127.0.0.1:4000",
        "model_placeholder": "gpt-4o-mini",
        "headers": {},
        "env_hint": "LITELLM_MASTER_KEY",
    },
    "GROQ": {
        "id": "GROQ",
        "label": "Groq",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "https://api.groq.com/openai/v1",
        "model_placeholder": "llama-3.3-70b-versatile",
        "headers": {},
        "env_hint": "GROQ_API_KEY",
    },
    "OLLAMA": {
        "id": "OLLAMA",
        "label": "Ollama (local)",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "http://127.0.0.1:11434/v1",
        "model_placeholder": "llama3.2",
        "headers": {},
        "env_hint": "",
    },
    "CUSTOM": {
        "id": "CUSTOM",
        "label": "Custom endpoint",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "",
        "model_placeholder": "my-model",
        "headers": {},
        "env_hint": "",
    },
}

PROTOCOL_TYPES = ("OPENAI_COMPATIBLE", "ANTHROPIC_COMPATIBLE", "NATIVE")
COMBO_TIERS = ("ECO", "BALANCED", "BEST")
ROUTE_TIERS = COMBO_TIERS

BATCH_TO_TIER = {"eco": "ECO", "normal": "BALANCED", "heavy": "BEST"}
TIER_TO_BATCH = {"ECO": "eco", "BALANCED": "normal", "BEST": "heavy"}
TIER_ALIASES = {
    "eco": "ECO",
    "eco-friendly": "ECO",
    "balanced": "BALANCED",
    "normal": "BALANCED",
    "heavy": "BEST",
    "heavy_20x": "BEST",
    "best": "BEST",
}


def normalize_tier(raw: str) -> str:
    text = (raw or "").strip().upper().replace("-", "_").replace(" ", "_")
    if text in COMBO_TIERS:
        return text
    if text == "NORMAL":
        return "BALANCED"
    if text in {"HEAVY_20X", "HEAVY20X"}:
        return "BEST"
    lowered = (raw or "").strip().lower()
    mapped = TIER_ALIASES.get(lowered)
    if mapped:
        return mapped
    raise ValueError("tier must be ECO, BALANCED, or BEST")


def tier_to_batch(tier: str) -> str:
    return TIER_TO_BATCH[normalize_tier(tier)]


def batch_to_tier(batch: str) -> str:
    return BATCH_TO_TIER.get((batch or "").strip().lower(), "BALANCED")


def get_preset(preset_id: str) -> Preset:
    key = (preset_id or "CUSTOM").strip().upper().replace(" ", "_").replace("-", "_")
    aliases = {
        "GOOGLE": "GEMINI",
        "GOOGLE_AI": "GOOGLE_AI_STUDIO",
        "QWEN": "ALIBABA",
        "TOGETHER": "LLAMA",
        "OPENCODE": "OPENCODE_ZEN",
        "NEWAPI": "NEW_API",
        "Z_AI": "ZAI",
        "Z.AI": "ZAI",
    }
    key = aliases.get(key, key)
    if key not in PRESETS:
        return PRESETS["CUSTOM"]
    return PRESETS[key]


def preset_public() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for preset in PRESETS.values():
        rows.append(
            {
                "id": preset["id"],
                "label": preset["label"],
                "protocol": preset["protocol"],
                "base_url": preset["base_url"],
                "model_placeholder": preset["model_placeholder"],
                "headers": dict(preset["headers"]),
                "env_hint": preset["env_hint"],
            }
        )
    return rows
