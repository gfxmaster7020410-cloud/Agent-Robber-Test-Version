from robber.tui.protocol import (
    WorkbenchState,
    classify_stream,
    format_elapsed,
    mutation_rows,
    pretty_mode,
)


def test_format_elapsed() -> None:
    assert format_elapsed(927) == "927ms"
    assert format_elapsed(3 * 60_000 + 55) == "3m 55ms"


def test_classify_stream_headers() -> None:
    assert classify_stream(role="user", content="scan files") == ("initiating", "Initiating")
    assert classify_stream(role="system", content="Routing to a live provider", stream_kind="ack") == (
        "ack",
        "Routing",
    )
    assert classify_stream(role="tool", content="$ python --version", stream_kind="terminal") == (
        "terminal",
        "Terminal",
    )
    assert classify_stream(role="error", content="boom") == ("error", "return error!")
    assert classify_stream(role="assistant", content="Wrote to pref.tsx")[0] == "writing"
    assert classify_stream(role="assistant", content="Hello — here is the answer.") == (
        "reply",
        "Reply",
    )


def test_pretty_mode_and_mutations() -> None:
    assert pretty_mode("plan") == "Plan"
    rows = mutation_rows(
        {
            "files": [
                {
                    "path": "src/core/agent_router.py",
                    "marker": "+",
                    "detail": "(+45, -0)",
                    "line": "[+] src/core/agent_router.py (+45, -0)",
                }
            ]
        }
    )
    assert rows[0]["line"] == "[+] src/core/agent_router.py (+45, -0)"


def test_ingest_state_snapshot() -> None:
    state = WorkbenchState()
    state.ingest_state(
        {
            "mode": "plan",
            "model": "Hybrid Multi-LLM",
            "control_mode": "autonomous",
            "hybrid": True,
            "busy": False,
            "workspace": "H:/proj",
            "ssot": {"files": 150, "chunks": 1225, "backend": "hash-json"},
            "mcp": {"enabled": 4, "servers": [{"id": "a", "label": "fs", "enabled": True}]},
            "tps": 3,
            "mutations": {"files": []},
            "loop": {
                "phase": "dispatch",
                "turn_tokens": 12,
                "max_tokens_per_turn": 80000,
                "stale_locks": 1,
                "session": {"engine": "GPT-X / Local"},
                "graph": {"nodes": 4, "head": {"version": "v1.2"}},
                "horizon": {"status": "running", "cursor": 3, "total": 12},
            },
        }
    )
    assert state.mode == "plan"
    assert state.hybrid is True
    assert state.ssot_files == 150
    assert state.ssot_chunks == 1225
    assert state.mcp_online == 4
    assert state.loop_phase == "dispatch"
    assert state.turn_tokens == 12
    assert state.stale_locks == 1
    assert state.graph_nodes == 4
    assert state.graph_head == "v1.2"
    assert state.horizon_status == "running"
    assert state.horizon_cursor == 3
    assert state.horizon_total == 12
