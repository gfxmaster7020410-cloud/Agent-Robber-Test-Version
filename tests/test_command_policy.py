"""Command auto-approval policy engine unit tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from robber.security.perimeter import agent_install_root
from robber.ssot_db import SsotDB
from robber.tools.command_policy import (
    TIER_1_AUTO_APPROVE,
    TIER_2_USER_CONFIRMATION,
    TIER_3_BLOCKED,
    CommandPolicyEngine,
)


def test_tier1_auto_approved_commands() -> None:
    engine = CommandPolicyEngine()
    for cmd in ("git status", "git diff", "pwd", "ls", "pytest", "python --version", "whoami"):
        eval_res = engine.evaluate(cmd)
        assert eval_res.tier == TIER_1_AUTO_APPROVE, f"Expected Tier 1 for `{cmd}`, got Tier {eval_res.tier}"
        assert eval_res.allowed is True
        assert eval_res.requires_confirmation is False


def test_tier2_mutating_requires_confirmation() -> None:
    engine = CommandPolicyEngine()
    for cmd in ("npm install", "pip install pytest", "git commit -m 'feat'", "cargo add serde", "curl https://example.com"):
        eval_res = engine.evaluate(cmd)
        assert eval_res.tier == TIER_2_USER_CONFIRMATION, f"Expected Tier 2 for `{cmd}`, got Tier {eval_res.tier}"
        assert eval_res.requires_confirmation is True
        assert eval_res.blocked is False


def test_tier3_high_risk_blocked_and_logged(tmp_path: Path) -> None:
    db_path = tmp_path / "ssot.db"
    db = SsotDB(db_path)
    engine = CommandPolicyEngine(workspace=tmp_path)

    for cmd in ("rm -rf /", "format C:", "sudo rm -rf /etc", "while true; do echo 1; done"):
        eval_res = engine.evaluate(cmd)
        assert eval_res.tier == TIER_3_BLOCKED, f"Expected Tier 3 for `{cmd}`, got Tier {eval_res.tier}"
        assert eval_res.blocked is True
        assert eval_res.allowed is False


def test_pipeline_chain_elevates_to_highest_tier() -> None:
    engine = CommandPolicyEngine()
    # Safe + Safe -> Tier 1
    safe_chain = engine.evaluate("git status && python --version")
    assert safe_chain.tier == TIER_1_AUTO_APPROVE

    # Safe + Mutating -> Tier 2
    mutating_chain = engine.evaluate("git status && npm install")
    assert mutating_chain.tier == TIER_2_USER_CONFIRMATION

    # Safe + Blocked -> Tier 3
    blocked_chain = engine.evaluate("git status && rm -rf /")
    assert blocked_chain.tier == TIER_3_BLOCKED


def test_redirect_to_protected_engine_file_is_tier3() -> None:
    engine = CommandPolicyEngine(workspace=agent_install_root())
    eval_res = engine.evaluate("echo malicious > robber/ssot_db.py")
    assert eval_res.tier == TIER_3_BLOCKED
    assert eval_res.blocked is True


def test_dynamic_trust_prefix_override() -> None:
    engine = CommandPolicyEngine()
    before = engine.evaluate("npm install pyright")
    assert before.tier == TIER_2_USER_CONFIRMATION

    engine.trust_prefix("npm install")
    after = engine.evaluate("npm install pyright")
    assert after.tier == TIER_1_AUTO_APPROVE

    engine.reset_trust()
    reset = engine.evaluate("npm install pyright")
    assert reset.tier == TIER_2_USER_CONFIRMATION


def test_interactive_approval_flow() -> None:
    engine = CommandPolicyEngine()

    async def _test() -> None:
        async def _approver():
            await asyncio.sleep(0.05)
            assert len(engine.list_pending()) == 1
            engine.approve()

        task = asyncio.create_task(_approver())
        approved = await engine.request_confirmation("npm install typescript", tier=2, timeout=2.0)
        await task
        assert approved is True

        async def _denier():
            await asyncio.sleep(0.05)
            engine.deny()

        task2 = asyncio.create_task(_denier())
        approved2 = await engine.request_confirmation("pip install torch", tier=2, timeout=2.0)
        await task2
        assert approved2 is False

    asyncio.run(_test())
