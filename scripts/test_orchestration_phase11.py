"""Phase 11 — orchestration DB, presets, protocol adapters, combo routing."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def _ok(message: str) -> None:
    print(f"ok: {message}")


async def main() -> None:
    from robber.engine.presets import (
        get_preset,
        normalize_tier,
        preset_public,
        tier_to_batch,
    )
    from robber.engine.protocols import merge_headers, openai_body, openai_url
    from robber.engine.model_manager import CustomModelManager

    if normalize_tier("eco") != "ECO":
        _fail("eco alias")
    if normalize_tier("HEAVY_20X") != "BEST":
        _fail("HEAVY_20X alias")
    if normalize_tier("NORMAL") != "BALANCED":
        _fail("NORMAL alias")
    if tier_to_batch("BEST") != "heavy":
        _fail("BEST maps to heavy batch")
    _ok("tier aliases")

    ids = {item["id"] for item in preset_public()}
    for required in (
        "ANTHROPIC",
        "OPENAI",
        "OPENROUTER",
        "OMNIROUTE",
        "NEW_API",
        "LITELLM",
        "GOOGLE_AI_STUDIO",
        "OPENCODE_ZEN",
        "CUSTOM",
    ):
        if required not in ids:
            _fail(f"missing preset {required}")
    if get_preset("qwen")["id"] != "ALIBABA":
        _fail("qwen alias")
    _ok("presets")

    url = openai_url("https://api.omniroute.ai/v1")
    if url != "https://api.omniroute.ai/v1/chat/completions":
        _fail(f"openai_url {url}")
    headers = merge_headers("OPENAI_COMPATIBLE", "sk-test", {"X-Title": "Agent Robber"})
    if headers.get("Authorization") != "Bearer sk-test":
        _fail("bearer header")
    if headers.get("X-Title") != "Agent Robber":
        _fail("custom header inject")
    if "sk-test" not in headers["Authorization"]:
        _fail("auth missing")
    body = openai_body("deepseek/deepseek-r1", [{"role": "user", "content": "hi"}], temperature=0.1)
    if body["model"] != "deepseek/deepseek-r1" or "messages" not in body:
        _fail("openai body")
    _ok("protocol adapters")

    with tempfile.TemporaryDirectory(prefix="robber-orch-") as raw:
        work = Path(raw)
        os.environ["ROBBER_WORKSPACE"] = str(work)
        from robber.db.database import Database

        db = Database(work / ".robber" / "accounts.db")
        await db.connect()
        manager = CustomModelManager(db)
        await manager.bootstrap()

        try:
            await db.add_model({"name": "bad", "custom_headers": "{not-json"})
            _fail("bad headers should raise")
        except Exception:
            _ok("reject bad headers JSON")

        mid = await manager.add_model(
            {
                "name": "Omni DeepSeek",
                "provider_preset": "OMNIROUTE",
                "protocol_type": "OPENAI_COMPATIBLE",
                "base_url": "https://api.omniroute.ai/v1",
                "model_string": "deepseek/deepseek-r1",
                "api_key": "sk-secret-never-echo",
                "custom_headers": {"X-Title": "Agent Robber"},
                "fallback_model_id": None,
            }
        )
        coder = await manager.add_model(
            {
                "name": "Flash coder",
                "provider_preset": "GROQ",
                "model_string": "llama-3.3-70b-versatile",
                "api_key": "gsk-local",
            }
        )
        general = await manager.add_model(
            {
                "name": "Haiku",
                "provider_preset": "ANTHROPIC",
                "protocol_type": "ANTHROPIC_COMPATIBLE",
                "model_string": "claude-haiku",
                "api_key": "sk-ant-local",
            }
        )
        rows = await manager.get_models()
        if any("api_key" in row for row in rows):
            _fail("api_key leaked in public rows")
        if any("sk-secret" in str(row) for row in rows):
            _fail("secret echoed in public rows")
        match = next(item for item in rows if item["id"] == mid)
        if not match["key_ready"] or match["custom_headers"].get("X-Title") != "Agent Robber":
            _fail("public model fields")
        _ok("encrypted key omitted from get_models")

        await manager.update_model(mid, {"is_active": False})
        rows = await manager.get_models(active_only=True)
        if any(item["id"] == mid for item in rows):
            _fail("inactive still listed")
        await manager.update_model(mid, {"is_active": True})
        _ok("update is_active")

        combo_id = await manager.create_combo_model(
            {
                "name": "Eco ensemble",
                "tier": "ECO",
                "reasoning_model_id": mid,
                "coding_model_ids": [coder, general, mid, coder],
                "general_model_ids": [general, coder],
            }
        )
        combos = await manager.get_combo_models()
        if len(combos) != 1 or combos[0]["id"] != combo_id:
            _fail("combo create")
        if manager.classify_task("Please refactor this TypeScript module") != "coding":
            _fail("coding classify")
        if manager.classify_task("Why this architecture trade-off?", mode="act") != "reasoning":
            _fail("reasoning classify")
        if manager.classify_task("hello there") != "general":
            _fail("general classify")
        if manager.classify_task("ship it", mode="plan") != "reasoning":
            _fail("plan mode is reasoning")
        manager.set_route_mode("ECO")
        engines = await manager.engines_for_prompt("refactor the python module")
        if not engines:
            _fail("combo engines empty")
        labels = [item.label for item in engines]
        if labels[0] != "Flash coder":
            _fail(f"coding pool should lead with round-robin, got {labels}")
        _ok("combo classify + pool")

        await manager.update_combo_model(combo_id, {"is_active": False})
        empty = await manager.engines_for_prompt("refactor the python module", tier="ECO")
        if empty:
            _fail("inactive combo still routing")
        _ok("combo deactivate")

        ok = await manager.delete_model(general)
        if not ok:
            _fail("delete")
        await db.close()

    print("PASS: orchestration phase 11")


if __name__ == "__main__":
    asyncio.run(main())
