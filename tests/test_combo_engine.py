import pytest
from unittest.mock import AsyncMock, MagicMock
from robber.engine.model_manager import CustomModelManager

@pytest.mark.asyncio
async def test_combo_failover():
    db = MagicMock()
    # Mock get_models to return a model that fails on probe
    db.get_models = AsyncMock(return_value=[
        {"id": "m1", "name": "Model 1", "fallback_model_id": "m2", "provider_type": "NATIVE"},
        {"id": "m2", "name": "Model 2", "fallback_model_id": None, "provider_type": "NATIVE"}
    ])
    
    manager = CustomModelManager(db)
    
    # Force probe to return False for m1
    manager._probe = AsyncMock(side_effect=lambda m: m["id"] != "m1")
    
    result = await manager.route_request("m1", {})
    assert result["model"] == "Model 2"
    assert result["status"] == "success"
