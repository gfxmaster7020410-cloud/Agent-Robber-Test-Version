from robber.events import attach_sync_envelope, mutation_sync_frame
from robber.orchestrator import (
    is_failover_error,
    is_rate_limited,
    is_transient_error,
    parse_dashboard_mode,
    step_action_command,
)


def test_parse_dashboard_modes() -> None:
    plan = parse_dashboard_mode("Plan Mode")
    assert plan["mode"] == "plan"
    assert plan["hybrid"] is False

    beast = parse_dashboard_mode("Beast Mode")
    assert beast["mode"] == "act"
    assert beast["control"] == "autonomous"
    assert beast["beast"] is True
    assert beast["tier"] == "BEST"

    hybrid = parse_dashboard_mode("Hybrid Multi-LLM")
    assert hybrid["hybrid"] is True
    assert hybrid["mode"] == "act"

    manual = parse_dashboard_mode("Manual")
    assert manual["control"] == "manual"


def test_step_actions_and_failover() -> None:
    assert step_action_command("undo") == "undo"
    assert step_action_command("accept_all") == "accept"
    assert step_action_command("revert-all") == "revert"
    assert step_action_command("redo_step") == "redo"

    class Rate:
        status_code = 429

        def __str__(self) -> str:
            return "Too Many Requests"

    assert is_rate_limited(Rate())  # type: ignore[arg-type]
    assert is_failover_error(TimeoutError("timed out"))

    class Unauthorized:
        status_code = 401

        def __str__(self) -> str:
            return "401 Unauthorized"

    class ServerDown:
        status_code = 500

        def __str__(self) -> str:
            return "500 Internal Server Error"

    class BadRequest:
        status_code = 400

        def __str__(self) -> str:
            return "BadRequestError"

    assert is_failover_error(Unauthorized())  # type: ignore[arg-type]
    assert is_failover_error(ServerDown())  # type: ignore[arg-type]
    assert is_failover_error(BadRequest())  # type: ignore[arg-type]
    assert is_failover_error(Exception("NotFoundError: model not found"))
    assert is_transient_error(TimeoutError("timed out"))
    assert is_transient_error(ServerDown())  # type: ignore[arg-type]
    assert not is_transient_error(Unauthorized())  # type: ignore[arg-type]
    assert not is_transient_error(BadRequest())  # type: ignore[arg-type]


def test_file_mutation_update_envelope() -> None:
    frame = mutation_sync_frame({"files": [], "pending": 0, "can_undo": False, "can_redo": False})
    assert frame["event"] == "FILE_MUTATION_UPDATE"
    assert frame["type"] == "mutations"
    assert frame["id"]

    stamped = attach_sync_envelope({"type": "mutations", "mutations": {"files": []}})
    assert stamped["event"] == "FILE_MUTATION_UPDATE"
    assert stamped["id"]
