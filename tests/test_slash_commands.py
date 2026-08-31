import pytest
from unittest.mock import AsyncMock, MagicMock
from robber.commands.handlers import cmd_model
from robber.commands.registry import ExecutionContext

@pytest.mark.asyncio
async def test_cmd_model_valid():
    ctx = MagicMock(spec=ExecutionContext)
    ctx.args = ["Gemini Ultra"]
    ctx.argline = "Gemini Ultra"
    ctx.host.list_models = MagicMock(return_value=["Gemini Ultra", "GPT-X / Local"])
    ctx.host.set_model = AsyncMock(return_value="Gemini Ultra")
    ctx.state = {}
    
    result = await cmd_model(ctx)
    assert result.ok is True
    assert "Gemini Ultra" in result.content

@pytest.mark.asyncio
async def test_cmd_model_invalid():
    ctx = MagicMock(spec=ExecutionContext)
    ctx.args = ["InvalidModel"]
    ctx.argline = "InvalidModel"
    ctx.host.list_models = MagicMock(return_value=["Gemini Ultra"])
    
    result = await cmd_model(ctx)
    assert result.ok is False
    assert "Unknown model" in result.content
