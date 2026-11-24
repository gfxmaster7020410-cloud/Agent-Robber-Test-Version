from __future__ import annotations

import asyncio
import json
from pathlib import Path

from robber.engine.discovery import (
    DiscoveryResult,
    ModelDiscoveryService,
    bind_discovery,
    classify_model_id,
    discover_and_ingest,
    discovery_log,
    failover_log,
    models_endpoint,
    parse_models_payload,
)
from robber.engine.failover_pool import FailoverPoolRegistry, bind_failover_pool, is_pinned_live_route
from robber.orchestrator import is_failover_error, is_rate_limited


OPENROUTER_PAYLOAD = {
    "data": [
        {
            "id": "deepseek/deepseek-r1:free",
            "pricing": {"prompt": "0", "completion": "0"},
        },
        {
            "id": "qwen/qwen-2.5-coder-32b-instruct:free",
            "pricing": {"prompt": "0", "completion": "0"},
        },
        {"id": "meta-llama/llama-3.3-70b-instruct", "pricing": {"prompt": "0.10"}},
        {"id": "openai/gpt-4o-mini", "prompt_cost": 0},
        {
            "id": "nvidia/nemotron-3.5-lightning:free",
            "pricing": {"prompt": "0", "completion": "0"},
        },
        {"id": "Qwen/Qwen2.5-Coder-32B-Instruct"},
        {"id": "nvidia/llama-3.1-nemotron-70b-instruct:free"},
    ]
}


def test_models_endpoint_normalization() -> None:
    assert models_endpoint("https://openrouter.ai/api/v1") == "https://openrouter.ai/api/v1/models"
    assert models_endpoint("https://api.groq.com/openai/v1/") == "https://api.groq.com/openai/v1/models"
    assert models_endpoint("http://127.0.0.1:4000") == "http://127.0.0.1:4000/v1/models"


def test_heuristic_classifier() -> None:
    reasoning = classify_model_id("deepseek/deepseek-r1:free")
    assert reasoning.type == "reasoning"
    assert reasoning.is_free is True

    coding = classify_model_id("qwen/qwen-2.5-coder-32b-instruct:free")
    assert coding.type == "coding"
    assert coding.scope == "coder"
    assert coding.is_free is True

    general = classify_model_id("meta-llama/llama-3.3-70b-instruct")
    assert general.type == "general"

    paid_free = classify_model_id("openai/gpt-4o-mini", pricing={"prompt_cost": 0})
    assert paid_free.is_free is True
    assert paid_free.type == "general"


def test_parse_and_logs() -> None:
    models = parse_models_payload(OPENROUTER_PAYLOAD)
    ids = [item.id for item in models]
    assert "deepseek/deepseek-r1:free" in ids
    assert "qwen/qwen-2.5-coder-32b-instruct:free" in ids
    assert discovery_log(5).startswith("✦ Initiating: Discovered 5 active models")
    assert "Auto-detected failure on [A]" in failover_log("A", "B")
    assert "Swapped to auto-discovered [B]" in failover_log("A", "B")


def test_discovery_cache_overrides_and_pool(tmp_path: Path) -> None:
    cache = tmp_path / "models_cache.json"
    seen_auth: list[str] = []

    def fake_fetch(url: str, headers: dict[str, str]) -> dict:
        seen_auth.append(headers.get("Authorization") or "")
        assert url.endswith("/v1/models") or url.endswith("/models")
        return OPENROUTER_PAYLOAD

    service = ModelDiscoveryService(cache_path=cache, fetcher=fake_fetch)
    bind_discovery(service)
    pool = bind_failover_pool(FailoverPoolRegistry())

    result = asyncio.run(
        discover_and_ingest(
            provider_id="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-test-secret",
            extra_headers={"X-Title": "Agent Robber"},
            force=True,
        )
    )
    assert result.ok
    assert result.active_count >= 4
    blob = cache.read_text(encoding="utf-8")
    assert "sk-test-secret" not in blob
    assert "Bearer" not in blob
    stored = json.loads(blob)
    assert "overrides" in stored
    assert "endpoints" in stored

    cached = asyncio.run(
        service.discover(
            provider_id="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-test-secret",
            force=False,
        )
    )
    assert cached.from_cache is True
    assert len(seen_auth) == 1

    service.set_override("qwen/qwen-2.5-coder-32b-instruct:free", disabled=True)
    service.set_override("nvidia/nemotron-3.5-lightning:free", priority_high=True)
    pool.hydrate_from_cache()
    coding = [item.model.id for item in pool.members_for("coding")]
    assert "qwen/qwen-2.5-coder-32b-instruct:free" not in coding
    assert "Qwen/Qwen2.5-Coder-32B-Instruct" not in coding
    reasoning = pool.engines_for("reasoning")
    assert reasoning
    assert "nemotron" in (reasoning[0].label or "").lower()
    assert all("qwen2.5-coder" not in f"{item.label} {item.model_string}".lower() for item in reasoning)

    pool.quarantine(reasoning[0].label, seconds=60)
    nxt = pool.next_after(reasoning[0], "reasoning", {reasoning[0].label})
    assert nxt is not None
    assert nxt.label != reasoning[0].label


def test_pinned_routes_reject_huggingface_qwen() -> None:
    assert is_pinned_live_route("openrouter", "nvidia/nemotron-3.5-lightning:free")
    assert is_pinned_live_route("groq", "llama-3.3-70b-versatile")
    assert not is_pinned_live_route("groq", "meta-llama/llama-prompt-guard-2-22m")
    assert is_pinned_live_route("gemini", "gemini-2.5-flash")
    assert not is_pinned_live_route("gemini", "models/gemini-flash-latest")
    assert not is_pinned_live_route("gemini", "models/gemini-flash-lite-latest")
    assert not is_pinned_live_route("gemini", "models/gemini-2.5-flash-preview-tts")
    assert not is_pinned_live_route("gemini", "models/gemini-2.5-flash-native-audio")
    assert not is_pinned_live_route("openrouter", "Qwen/Qwen2.5-Coder-32B-Instruct")
    assert not is_pinned_live_route("huggingface", "Hugging Face Instruct")
    assert not is_pinned_live_route("groq", "Qwen/Qwen2.5-Coder-7B-Instruct")


def test_failover_http_statuses() -> None:
    class Unauthorized:
        status_code = 401

        def __str__(self) -> str:
            return "Error code: 401 Unauthorized"

    class Server:
        status_code = 503

        def __str__(self) -> str:
            return "Bad gateway 503"

    assert is_failover_error(Unauthorized())  # type: ignore[arg-type]
    assert is_failover_error(Server())  # type: ignore[arg-type]
    assert is_failover_error(Exception("BadRequestError: 400"))
    assert is_rate_limited(Unauthorized()) is False  # type: ignore[arg-type]


def test_discovery_result_public_omits_disabled() -> None:
    from robber.engine.discovery import DiscoveredModel

    result = DiscoveryResult(
        "openrouter",
        "https://openrouter.ai/api/v1",
        [
            DiscoveredModel("keep", True, "reasoning"),
            DiscoveredModel("drop", True, "coding", disabled=True),
        ],
    )
    public = result.public()
    ids = [item["id"] for item in public["discovered_models"]]
    assert ids == ["keep"]
