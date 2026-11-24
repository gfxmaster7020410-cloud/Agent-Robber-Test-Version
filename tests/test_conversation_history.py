import asyncio
from pathlib import Path

from cryptography.fernet import Fernet

from robber.db.database import SCHEMA_SQL, Database


def test_schema_defines_conversation_history() -> None:
    assert "CREATE TABLE IF NOT EXISTS conversation_history" in SCHEMA_SQL
    for column in ("id", "session_id", "role", "content", "tokens", "timestamp"):
        assert column in SCHEMA_SQL


def test_conversation_history_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ROBBER_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("ROBBER_ACCOUNT_KEY", Fernet.generate_key().decode())

    async def go() -> None:
        db = Database(path=tmp_path / "accounts.db")
        await db.connect()
        try:
            first = await db.add_conversation_message(
                "sess-a",
                "user",
                "scan the workspace",
                tokens=5,
            )
            second = await db.add_conversation_message(
                "sess-a",
                "assistant",
                "found three config files",
            )
            other = await db.add_conversation_message("sess-b", "user", "other session")
            assert first > 0 and second > first
            assert other > 0

            turns = await db.get_conversation_history("sess-a")
            assert [row["role"] for row in turns] == ["user", "assistant"]
            assert turns[0]["content"] == "scan the workspace"
            assert turns[0]["tokens"] == 5
            assert turns[1]["content"] == "found three config files"
            assert turns[1]["tokens"] > 0
            assert all(row["session_id"] == "sess-a" for row in turns)
            assert all(row["timestamp"] for row in turns)

            isolated = await db.get_conversation_history("sess-b")
            assert len(isolated) == 1
            assert isolated[0]["content"] == "other session"

            await db.clear_conversation_history("sess-a")
            assert await db.get_conversation_history("sess-a") == []
            leftover = await db.get_conversation_history("sess-b")
            assert len(leftover) == 1
        finally:
            await db.close()

    asyncio.run(go())
