import asyncio

from robber.agent_loop import AgentLoopManager
from robber.mcp.host import McpHost
from robber.orchestrator import is_transient_error


class _DeadStdio:
    status = "error"
    alive = False
    error = "exit 1"

    async def ping(self, timeout: float = 5.0) -> bool:
        return False

    async def stop(self) -> None:
        return None


class _LiveStdio:
    status = "ready"
    alive = True
    error = ""

    async def ping(self, timeout: float = 5.0) -> bool:
        return True


def test_watchdog_restarts_dead_stdio(monkeypatch) -> None:
    host = McpHost()
    host._started = True
    called: list[str] = []

    async def ensure(server_id: str) -> None:
        called.append(server_id)

    monkeypatch.setattr(host, "_ensure_server", ensure)
    monkeypatch.setattr(
        "robber.mcp.host.load_catalog",
        lambda: [
            {"id": "echo-stdio", "kind": "stdio", "enabled": True},
            {"id": "jailed-fs", "kind": "builtin", "enabled": True},
            {"id": "off-stdio", "kind": "stdio", "enabled": False},
        ],
    )
    host._stdio["echo-stdio"] = _DeadStdio()  # type: ignore[assignment]

    async def go() -> None:
        restarted = await host.watchdog_tick()
        assert restarted == 1
        assert called == ["echo-stdio"]
        host._stdio["echo-stdio"] = _LiveStdio()  # type: ignore[assignment]
        assert await host.watchdog_tick() == 0
        assert called == ["echo-stdio"]

    asyncio.run(go())


def test_turn_retries_transient_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr("robber.config.TURN_RETRY_BASE_SECONDS", 0)
    loop = AgentLoopManager(hub=None)
    calls = {"n": 0}

    async def flaky(_query: str) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("timed out")

    async def dispatch(_sender, _raw: str) -> None:
        return None

    loop.bind_runtime(run_turn=flaky, dispatch=dispatch)
    loop.healer = None

    asyncio.run(loop._run_agent_turn("hello"))
    assert calls["n"] == 3


def test_turn_does_not_retry_auth_errors() -> None:
    loop = AgentLoopManager(hub=None)
    calls = {"n": 0}

    class Unauthorized(Exception):
        status_code = 401

    async def boom(_query: str) -> None:
        calls["n"] += 1
        raise Unauthorized("nope")

    async def dispatch(_sender, _raw: str) -> None:
        return None

    loop.bind_runtime(run_turn=boom, dispatch=dispatch)
    loop.healer = None

    try:
        asyncio.run(loop._run_agent_turn("hello"))
        raise AssertionError("expected auth error")
    except Unauthorized:
        pass
    assert calls["n"] == 1
    assert is_transient_error(TimeoutError("x"))
    assert not is_transient_error(Unauthorized("x"))
