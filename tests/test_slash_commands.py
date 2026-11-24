import pytest
from unittest.mock import AsyncMock, MagicMock
from robber.commands.handlers import cmd_model

def _ctx(**overrides):
    host = MagicMock()
    ctx = MagicMock()
    ctx.args = []
    ctx.argline = ""
    ctx.state = {}
    ctx.host = host
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


@pytest.mark.asyncio
async def test_cmd_model_valid():
    ctx = _ctx(args=["Gemini Ultra"], argline="Gemini Ultra")
    ctx.host.list_models = MagicMock(return_value=["Gemini Ultra", "GPT-X / Local"])
    ctx.host.set_model = AsyncMock(return_value="Gemini Ultra")

    result = await cmd_model(ctx)
    assert result.ok is True
    assert "Gemini Ultra" in result.content


@pytest.mark.asyncio
async def test_cmd_model_invalid():
    ctx = _ctx(args=["InvalidModel"], argline="InvalidModel")
    ctx.host.list_models = MagicMock(return_value=["Gemini Ultra"])

    result = await cmd_model(ctx)
    assert result.ok is False
    assert "Unknown model" in result.content
