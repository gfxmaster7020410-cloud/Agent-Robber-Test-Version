from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from robber.planner import AutonomousPlanner, CODE_GEN, FAST_DRAFT, REASONING
from robber.router import (
    APIKeyRotator,
    FailoverMatrix,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ProviderError,
    SessionFailover,
    UnifiedProviderProxy,
    assign_engine,
    classify_strength,
)
from robber.router.model_router import RouteDecision, RouteTarget
from robber.ssot_db import reset_ssot_cache
from robber.state_manager import StateManager


@pytest.fixture(autouse=True)
def _close_ssot() -> None:
    yield
    reset_ssot_cache()


async def _noop_sleep(_seconds: float) -> None:
    return None


class ScriptedProxy:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, str, str | None]] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls.append((request.provider, request.model, request.api_key))
        if not self.outcomes:
            raise ProviderError("script exhausted", status_code=500, kind="server")
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]


class StubRouter:
    def choose(self, task=None, *, prompt: str = "", task_type: str | None = None, prefer: str | None = None) -> RouteDecision:
        return RouteDecision(
            task_type=task_type or CODE_GEN,
            primary=RouteTarget("primary", "openai", "gpt-test", context_length=8000),
            fallbacks=[RouteTarget("fallback", "gemini", "gemini-flash", context_length=32000)],
            prompt=prompt or "implement the change",
            tokens=4,
        )


class FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class FakeClient:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self.payload = payload
        self.urls: list[str] = []

    async def post(self, url: str, json=None, headers=None, params=None) -> FakeResponse:
        self.urls.append(url)
        return FakeResponse(self.status_code, self.payload)


def _keys(provider: str) -> list[str]:
    return {
        "openai": ["ok-primary", "ok-secondary"],
        "gemini": ["gk-1"],
        "anthropic": ["ak-1"],
    }.get(provider, [])


def test_package_still_exports_session_router() -> None:
    assert classify_strength("implement the refactor for this module") == "codegen"
    engine = assign_engine("reindex the mmap fingerprint", "GPT-X / Local")
    assert engine.label
    session = SessionFailover("keep")
    assert session.session_id == "keep"


def test_model_router_uses_planner_task_type(tmp_path: Path) -> None:
    plan = AutonomousPlanner(tmp_path).plan("Refactor state graph and update tests")
    by_tag = {task.tag: task for task in plan.tasks}
    assert by_tag["inspect"].task_type == REASONING
    assert by_tag["mutate"].task_type == CODE_GEN
    assert by_tag["verify"].task_type == FAST_DRAFT
    router = ModelRouter()
    inspect = router.choose(
        {"payload_json": {"tag": "inspect", "task_type": REASONING}},
        prompt="architect the state graph",
    )
    mutate = router.choose(
        {"payload_json": {"tag": "mutate", "task_type": CODE_GEN}},
        prompt="patch robber/state_graph.py",
    )
    draft = router.choose(task_type=FAST_DRAFT, prompt="ok")
    assert inspect.task_type == REASONING
    assert inspect.primary.label == "Claude Opus 4"
    assert mutate.task_type == CODE_GEN
    assert mutate.fallbacks
    assert draft.primary.label == "GPT-X / Local"
    assert draft.cost_score == 0.0
    long_prompt = "word " * 20_000
    fitted = router.choose(task_type=FAST_DRAFT, prompt=long_prompt)
    assert fitted.clipped is True
    assert fitted.tokens < 12_000


def test_key_rotator_cools_down_after_429() -> None:
    clock = {"t": 10.0}
    rotator = APIKeyRotator(keys_fn=_keys, clock=lambda: clock["t"], rpm=8, cooldown=5.0)
    first = rotator.acquire("openai")
    second = rotator.acquire("openai")
    assert first is not None and second is not None
    assert first.secret == "ok-primary"
    assert second.secret == "ok-secondary"
    rotator.cooldown(first)
    third = rotator.acquire("openai")
    assert third is not None
    assert third.index == second.index
    assert rotator.cooling("openai")
    snap = rotator.snapshot()
    assert all("ok-primary" not in str(row) for row in snap["slots"])
    clock["t"] = 20.0
    revived = rotator.acquire("openai")
    assert revived is not None
    assert revived.index == first.index


def test_proxy_parses_openai_payload() -> None:
    client = FakeClient(
        200,
        {
            "model": "gpt-test",
            "choices": [{"message": {"content": "hello from proxy"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        },
    )
    proxy = UnifiedProviderProxy(client=client)

    async def go() -> ModelResponse:
        return await proxy.generate(
            ModelRequest(prompt="hi", model="gpt-test", provider="openai", api_key="ok-primary")
        )

    response = asyncio.run(go())
    assert response.text == "hello from proxy"
    assert response.total_tokens == 8
    assert response.finish_reason == "stop"
    assert client.urls


def test_proxy_raises_on_http_error() -> None:
    client = FakeClient(500, {"error": {"message": "upstream down"}})
    proxy = UnifiedProviderProxy(client=client)

    async def go() -> None:
        await proxy.generate(ModelRequest(prompt="hi", model="gpt-test", provider="openai", api_key="ok-primary"))

    with pytest.raises(ProviderError) as caught:
        asyncio.run(go())
    assert caught.value.status_code == 500
    assert caught.value.kind == "server"


def test_failover_429_rotates_key_then_recovers(tmp_path: Path) -> None:
    manager = StateManager(tmp_path)
    task = manager.put_task("codegen hop", payload={"tag": "mutate", "task_type": CODE_GEN})
    proxy = ScriptedProxy(
        [
            ProviderError("HTTP 429: rate limit", status_code=429, provider="openai", kind="rate_limit"),
            ModelResponse(text="recovered", model="gpt-test", provider="openai", total_tokens=6),
        ]
    )
    matrix = FailoverMatrix(
        tmp_path,
        router=StubRouter(),
        proxy=proxy,
        rotator=APIKeyRotator(keys_fn=_keys, rpm=20, cooldown=30.0),
        manager=manager,
        sleep=_noop_sleep,
    )
    result = asyncio.run(matrix.execute("implement the change", task=task))
    assert result.response is not None
    assert result.response.text == "recovered"
    assert result.switched is True
    assert proxy.calls[0][2] == "ok-primary"
    assert proxy.calls[1][2] == "ok-secondary"
    assert matrix.rotator.cooling("openai")
    decisions = manager.db.list_decisions(manager.project_id, branch="router")
    assert any(row.get("version_tag") == "failover" for row in decisions)
    blob = " ".join(str(row.get("rationale") or "") + str(row.get("payload") or "") for row in decisions)
    assert "ok-primary" not in blob
    assert "ok-secondary" not in blob


def test_failover_500_switches_provider_without_crashing(tmp_path: Path) -> None:
    manager = StateManager(tmp_path)
    task = manager.put_task("server hop", payload={"tag": "mutate", "task_type": CODE_GEN})
    proxy = ScriptedProxy(
        [
            ProviderError("HTTP 500: boom", status_code=500, provider="openai", kind="server"),
            ModelResponse(text="from gemini", model="gemini-flash", provider="gemini", total_tokens=4),
        ]
    )
    matrix = FailoverMatrix(
        tmp_path,
        router=StubRouter(),
        proxy=proxy,
        rotator=APIKeyRotator(keys_fn=_keys, rpm=20),
        manager=manager,
        sleep=_noop_sleep,
    )
    result = asyncio.run(matrix.execute("implement the change", task=task))
    assert result.response is not None
    assert result.response.provider == "gemini"
    assert result.switched is True
    assert [call[0] for call in proxy.calls] == ["openai", "gemini"]
    decisions = manager.db.list_decisions(manager.project_id)
    assert any("failover" in str(row.get("choice") or "") for row in decisions)
    assert any(row.get("version_tag") == "failover" for row in decisions)
    fail_proxy = ScriptedProxy(
        [
            ProviderError("HTTP 500", status_code=500, provider="openai", kind="server"),
            ProviderError("HTTP 500", status_code=500, provider="gemini", kind="server"),
        ]
    )
    failing = FailoverMatrix(
        tmp_path,
        router=StubRouter(),
        proxy=fail_proxy,
        rotator=APIKeyRotator(keys_fn=_keys, rpm=20),
        manager=manager,
        sleep=_noop_sleep,
    )
    dead = asyncio.run(failing.execute("still implement", task=task))
    assert dead.response is None
    assert dead.switched is True
    assert dead.events
    assert dead.error.startswith("All API providers failed:")
    rows = manager.list_tasks()
    assert any(row["id"] == task["id"] for row in rows)


def test_active_keys_are_fingerprints_not_secrets() -> None:
    rotator = APIKeyRotator(keys_fn=_keys, rpm=20)
    inventory = rotator.get_active_keys()
    blob = json.dumps(inventory)
    assert "ok-primary" not in blob
    assert "gk-1" not in blob
    assert inventory["openai"]["ready"] is True
    assert inventory["openai"]["count"] == 2
    assert inventory["ollama"]["local"] is True
    assert rotator.acquire("fallback") is None


def test_failover_stops_after_three_network_hops(tmp_path: Path) -> None:
    class QuadRouter:
        def choose(self, task=None, *, prompt: str = "", task_type: str | None = None, prefer: str | None = None):
            return RouteDecision(
                task_type=task_type or CODE_GEN,
                primary=RouteTarget("a", "openai", "m1"),
                fallbacks=[
                    RouteTarget("b", "openai", "m2"),
                    RouteTarget("c", "gemini", "m3"),
                    RouteTarget("d", "anthropic", "m4"),
                ],
                prompt=prompt or "implement the change",
                tokens=4,
            )

    proxy = ScriptedProxy(
        [
            ProviderError("HTTP 500", status_code=500, provider="openai", kind="server"),
            ProviderError("HTTP 500", status_code=500, provider="openai", kind="server"),
            ProviderError("HTTP 500", status_code=500, provider="gemini", kind="server"),
            ProviderError("HTTP 500", status_code=500, provider="anthropic", kind="server"),
        ]
    )
    matrix = FailoverMatrix(
        tmp_path,
        router=QuadRouter(),
        proxy=proxy,
        rotator=APIKeyRotator(keys_fn=_keys, rpm=20),
        manager=StateManager(tmp_path),
        sleep=_noop_sleep,
    )
    dead = asyncio.run(matrix.execute("still implement"))
    assert dead.response is None
    assert len(proxy.calls) == 3
    assert dead.error.startswith("All API providers failed:")
    assert dead.exhaustion is not None
    assert dead.public()["exhausted"] is True


def test_failover_skips_http_when_keys_missing(tmp_path: Path) -> None:
    from robber.local.ollama_adapter import OllamaAdapter, bind_adapter
    from robber.router import ProviderExhaustionError

    bind_adapter(OllamaAdapter(probe=lambda _url: None))
    proxy = ScriptedProxy(
        [ModelResponse(text="should-not-run", model="gpt-test", provider="openai", total_tokens=1)]
    )
    matrix = FailoverMatrix(
        tmp_path,
        router=StubRouter(),
        proxy=proxy,
        rotator=APIKeyRotator(keys_fn=lambda _p: ["", "  ", "changeme"], rpm=20),
        manager=StateManager(tmp_path),
        sleep=_noop_sleep,
    )
    dead = asyncio.run(matrix.execute("implement the change"))
    assert proxy.calls == []
    assert dead.response is None
    assert dead.exhaustion is not None
    assert isinstance(dead.exhaustion, ProviderExhaustionError)
    assert dead.error.startswith("All API providers failed:")


def test_rotator_drops_empty_and_placeholder_keys() -> None:
    rotator = APIKeyRotator(
        keys_fn=lambda provider: {
            "openai": ["", "  ", "changeme", "sk-live-ok"],
            "gemini": ["none"],
            "groq": [],
        }.get(provider, []),
        rpm=20,
    )
    assert rotator.has_usable_key("openai") is True
    assert rotator.has_usable_key("gemini") is False
    assert rotator.has_usable_key("groq") is False
    lease = rotator.acquire("openai")
    assert lease is not None
    assert lease.secret == "sk-live-ok"
    assert rotator.acquire("gemini") is None
    inventory = rotator.get_active_keys()
    assert inventory["openai"]["count"] == 1
    assert "sk-live-ok" not in json.dumps(inventory)
