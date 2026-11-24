from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import api_server


def test_legacy_api_schemas_match_core(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(api_server, "DB_PATH", tmp_path / "legacy.db")
    monkeypatch.setenv("ROBBER_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("ROBBER_ACCOUNT_KEY", Fernet.generate_key().decode())
    api_server._init_db()
    with api_server._db() as conn:
        conn.execute(
            """
            INSERT INTO conversation_history (session_id, role, content, tokens, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("current_session", "user", "hello core schema", 4, "2026-09-03T00:00:00+00:00"),
        )
        conn.execute(
            """
            INSERT INTO ssot_context (id, window_tokens, packed_tokens, hit_count, updated_at)
            VALUES (1, 200000, 1200, 3, '2026-09-03T00:00:00+00:00')
            """
        )

    client = TestClient(api_server.app)
    history = client.get("/api/history").json()
    assert history["session_id"] == "current_session"
    assert history["turns"]
    turn = history["turns"][0]
    for key in ("id", "session_id", "role", "content", "tokens", "timestamp"):
        assert key in turn
    assert turn["content"] == "hello core schema"
    assert turn["tokens"] == 4

    ssot = client.get("/api/ssot/window").json()
    for key in (
        "files",
        "chunks",
        "backend",
        "watching",
        "watch_events",
        "window",
        "window_label",
        "packed",
        "available",
        "hits",
    ):
        assert key in ssot
    assert ssot["window"] == 200000
    assert ssot["packed"] == 1200
    assert ssot["hits"] == 3

    trillion = client.post("/api/ssot/window", json={"window": "1T"}).json()
    assert trillion["window"] == 1_000_000_000_000
    assert trillion["window_label"] == "1T"

    root = client.get("/").json()
    assert "ssot" in root
    assert "window" in root["ssot"]

    health = client.get("/api/v1/health").json()
    assert health["status"] == "ok"
    assert "ssot" in health
    assert "*" not in api_server._cors_origins()
