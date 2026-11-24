"""30ms WebSocket stream throttle and per-turn token caps."""

from __future__ import annotations

from robber.agent_loop import AgentLoopManager
from robber.config import MAX_TOKENS_PER_TURN, STREAM_FLUSH_MS
from robber.orchestrator import StreamThrottle, is_transient_error


def test_stream_throttle_holds_until_window() -> None:
    throttle = StreamThrottle(window_ms=30, max_chars=400)
    assert STREAM_FLUSH_MS == 30
    assert throttle.feed("a", now=1.0) == ""
    assert throttle.feed("b", now=1.020) == ""
    assert throttle.feed("c", now=1.031) == "abc"
    assert throttle.flush() == ""


def test_stream_throttle_flushes_on_max_chars() -> None:
    throttle = StreamThrottle(window_ms=1000, max_chars=32)
    blob = "x" * 32
    assert throttle.feed(blob, now=10.0) == blob


def test_turn_token_cap_pauses_and_resets() -> None:
    loop = AgentLoopManager(hub=None, max_tokens_per_turn=MAX_TOKENS_PER_TURN)
    assert loop.max_tokens_per_turn == 80_000
    loop.reset_turn_budget()
    assert loop.consume_tokens(80_000) is False
    assert loop.budget_exceeded() is True
    assert loop.tree()["token_cap_hit"] is True
    loop.reset_turn_budget()
    assert loop.consume_tokens(1) is True
    assert loop.budget_exceeded() is False


def test_custom_token_cap_and_permanent_errors_stay_non_transient() -> None:
    loop = AgentLoopManager(hub=None, max_tokens_per_turn=10)
    loop.reset_turn_budget()
    assert loop.consume_tokens(9) is True
    assert loop.consume_tokens(1) is False

    class Unauthorized:
        status_code = 401

        def __str__(self) -> str:
            return "401 Unauthorized"

    class BadRequest:
        status_code = 400

        def __str__(self) -> str:
            return "400 Bad Request"

    assert not is_transient_error(Unauthorized())  # type: ignore[arg-type]
    assert not is_transient_error(BadRequest())  # type: ignore[arg-type]
