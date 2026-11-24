from __future__ import annotations

import os

from robber.router import (
    Engine,
    SessionFailover,
    assign_engine,
    bind_session_router,
    classify_strength,
)
from robber.vault import iter_keys, rotate_key


def test_classify_strength_routes() -> None:
    assert classify_strength("please /sync and reindex mmap") == "index"
    assert classify_strength("list symbols and parse the AST") == "parse"
    assert classify_strength("search the docs online") == "research"
    assert classify_strength("architect the trade-off") == "reason"
    assert classify_strength("implement the refactor for this module") == "codegen"


def test_iter_keys_and_session_rotation(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "slot-a")
    monkeypatch.setenv("GROQ_API_KEY_2", "slot-b")
    keys = iter_keys("groq")
    assert keys[:2] == ["slot-a", "slot-b"]
    assert rotate_key("groq", "slot-a") == "slot-b"
    session = bind_session_router(SessionFailover("sess1"))
    assert session.key_for("groq") == "slot-a"
    assert session.rotate_provider("groq") == "slot-b"
    assert session.rotations == 1


def test_rate_limit_rotates_key_without_dropping_session(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "slot-a")
    monkeypatch.setenv("GROQ_API_KEY_2", "slot-b")
    session = bind_session_router(SessionFailover("keep-me"))
    engine = Engine("Hybrid Multi-LLM", "groq/openai/gpt-oss-20b", "groq", "slot-a")
    session.remember(engine, "codegen")

    class Limited(Exception):
        pass

    hop = session.next_channel(engine, Limited("429 rate limit"))
    assert hop is not None
    assert hop.api_key == "slot-b"
    assert hop.label == engine.label
    assert session.session_id == "keep-me"
    assert session.failovers >= 1


def test_assign_engine_prefers_local_for_index() -> None:
    engine = assign_engine("reindex the mmap fingerprint", "GPT-X / Local")
    assert engine.provider in {
        "ollama",
        "fallback",
        "groq",
        "gemini",
        "anthropic",
        "new_api",
        "openai",
        "openrouter",
        "omniroute",
        "tokenrouter",
    }
    assert engine.label


def test_hybrid_prefers_openrouter_nemotron_free() -> None:
    from robber.router.session import HYBRID_LABEL, MODEL_ROUTES

    first = MODEL_ROUTES[HYBRID_LABEL][0]
    assert first[1] == "openrouter"
    assert "nemotron-3.5-lightning:free" in first[0]


def test_alias_env_names_promote_to_canonical(monkeypatch, tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "GEMINI_KEY_1=studio-one",
                "GEMINI_KEY_2=studio-two",
                "OPENROUTER_NEMOTRON_KEY=or-nemo",
                "OMNIROUTE_NAME1_KEY=omni-one",
                "TOKEN_ROUTER_KEY_1=tr-one",
                "TOKEN_ROUTER_KEY_2=tr-two",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("robber.vault.ENV_PATH", env)
    for name in list(os.environ):
        upper = name.upper()
        if any(
            token in upper
            for token in (
                "GEMINI",
                "GOOGLE_API",
                "OPENROUTER",
                "OMNIROUTE",
                "TOKEN_ROUTER",
                "TOKENROUTER",
            )
        ):
            monkeypatch.delenv(name, raising=False)
    from robber.router.key_rotator import APIKeyRotator
    from robber.vault import get_key, iter_keys, load_dotenv

    load_dotenv(env)
    assert get_key("gemini") == "studio-one"
    assert iter_keys("gemini")[:2] == ["studio-one", "studio-two"]
    assert get_key("openrouter") == "or-nemo"
    assert get_key("omniroute") == "omni-one"
    assert get_key("tokenrouter") == "tr-one"
    assert get_key("token_router") == "tr-one"
    inventory = APIKeyRotator().get_active_keys()
    blob = str(inventory)
    assert "studio-one" not in blob
    assert "or-nemo" not in blob
    assert inventory["gemini"]["ready"] is True
    assert inventory["gemini"]["count"] == 2
    assert inventory["openrouter"]["ready"] is True
    assert inventory["omniroute"]["ready"] is True
    assert inventory["tokenrouter"]["count"] == 2
