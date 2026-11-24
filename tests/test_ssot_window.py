from robber.config import DEFAULT_CONTEXT_WINDOW, SSOT_PACK_TOKEN_CAP
from robber.ssot_context import (
    clip_to_token_budget,
    format_window,
    parse_context_shorthand,
    parse_window,
    set_context_window,
    turn_pack_window,
    working_window,
)


def test_default_working_context_is_200k() -> None:
    assert DEFAULT_CONTEXT_WINDOW == 200_000
    assert parse_window("200k") == 200_000
    assert format_window(DEFAULT_CONTEXT_WINDOW) == "200K"


def test_shorthand_is_unbounded() -> None:
    assert parse_window("1M") == 1_000_000
    assert parse_window("1m") == 1_000_000
    assert parse_window("1B") == 1_000_000_000
    assert parse_window("100B") == 100_000_000_000
    assert parse_window("1t") == 1_000_000_000_000
    assert parse_window(1_000_000_000_000) == 1_000_000_000_000
    assert parse_window("1e12") == 1_000_000_000_000
    assert parse_context_shorthand("1T") == 1_000_000_000_000
    assert parse_context_shorthand("not-a-window") == DEFAULT_CONTEXT_WINDOW
    assert format_window(1_000_000) == "1M"
    assert format_window(1_000_000_000) == "1B"
    assert format_window(1_000_000_000_000) == "1T"


def test_working_window_does_not_clamp_unless_requested() -> None:
    assert working_window(1_000_000_000_000, model_capacity=128_000) == 1_000_000_000_000
    assert working_window(200_000, model_capacity=128_000, enforce_cap=True) == 115_200
    assert working_window(32_000, model_capacity=128_000, enforce_cap=True) == 32_000
    assert working_window(50, model_capacity=None) == 50
    assert turn_pack_window(1_000_000_000) == SSOT_PACK_TOKEN_CAP
    assert turn_pack_window(12_000) == 12_000
    assert 40_000 <= SSOT_PACK_TOKEN_CAP <= 50_000
    assert set_context_window("1T") == 1_000_000_000_000
    assert set_context_window("200k", enforce_cap=True, model_capacity=128_000) == 115_200


def test_clip_to_token_budget() -> None:
    blob = "word " * 200
    clipped = clip_to_token_budget(blob, 10)
    assert "clipped at token budget" in clipped
    assert len(clipped) < len(blob)


def test_reject_non_positive_windows() -> None:
    try:
        parse_window(0)
        raise AssertionError("zero window should be rejected")
    except ValueError:
        pass
    try:
        parse_window(-8)
        raise AssertionError("negative window should be rejected")
    except ValueError:
        pass


def test_schema_uses_bigint_token_columns() -> None:
    from robber.db.database import SCHEMA_SQL

    assert "window_tokens BIGINT" in SCHEMA_SQL
    assert "packed_tokens BIGINT" in SCHEMA_SQL


def test_sqlite_stores_trillion_token_window(tmp_path, monkeypatch) -> None:
    import asyncio

    from cryptography.fernet import Fernet

    from robber.db.database import Database

    monkeypatch.setenv("ROBBER_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("ROBBER_ACCOUNT_KEY", Fernet.generate_key().decode())

    async def go() -> None:
        db = Database(path=tmp_path / "accounts.db")
        await db.connect()
        try:
            stored = await db.set_ssot_window(1_000_000_000_000)
            assert stored == 1_000_000_000_000
            assert await db.get_ssot_window() == 1_000_000_000_000
        finally:
            await db.close()

    asyncio.run(go())
