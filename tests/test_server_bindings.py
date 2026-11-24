from robber.engine.worker_pool import WorkerPool


def test_cors_rejects_wildcard_and_keeps_local_hosts(monkeypatch) -> None:
    monkeypatch.setenv("ROBBER_CORS_ORIGINS", "*,http://127.0.0.1:9999")
    from server import _cors_origins

    origins = _cors_origins()
    assert "*" not in origins
    assert "http://127.0.0.1:5173" in origins
    assert "http://localhost:5173" in origins
    assert "http://127.0.0.1:9999" in origins


def test_runtime_bindings_and_ssot_payload_defaults() -> None:
    import server

    assert isinstance(server.worker_pool, WorkerPool)
    assert server.agent_loop.worker_pool is server.worker_pool
    assert server.ssot_watcher is not None
    assert server.agent_loop._run_turn is server.run_turn
    assert server.agent_loop._dispatch is server.dispatch

    payload = server.ssot_payload()
    assert payload["files"] >= 0
    assert payload["chunks"] >= 0
    assert "watching" in payload
    assert "watch_events" in payload
    assert payload.get("backend")

    snap = server.snapshot()
    assert snap["type"] == "state"
    assert isinstance(snap["timestamp"], (int, float))
    assert snap["ssot"]["watching"] in {True, False}
