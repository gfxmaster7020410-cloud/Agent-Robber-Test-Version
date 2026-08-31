"""Agent Robber — Backend API Server (Module 01).

Lightweight FastAPI server on http://127.0.0.1:8765.
Provides REST endpoints for model CRUD with encrypted vault storage.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from agent_robber.db.database import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("agent-robber")

# ---------------------------------------------------------------------------
# Shared database instance (initialised at startup)
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).resolve().parent.parent.parent / ".robber" / "agent_robber.db"
db = Database(DB_PATH)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Agent Robber API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup() -> None:
    await db.connect()
    log.info("Database connected at %s", DB_PATH)


@app.on_event("shutdown")
async def _shutdown() -> None:
    await db.close()


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class ModelCreate(BaseModel):
    alias: str = Field(..., min_length=1, description="Unique human-friendly alias")
    provider: str = Field(..., min_length=1, description="Provider name (openai, anthropic, …)")
    model_string: str = Field(..., min_length=1, description="Model identifier string")
    base_url: str = Field(default="", description="API base URL override")
    api_key: str = Field(default="", description="Plaintext API key (encrypted before storage)")
    protocol: str = Field(default="openai", description="Protocol: openai | anthropic")
    priority: int = Field(default=1, ge=1, le=100)
    weight: int = Field(default=50, ge=1, le=100)
    is_active: bool = True


# ---------------------------------------------------------------------------
# Routes — Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "db_connected": db.connected,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Routes — Models CRUD
# ---------------------------------------------------------------------------

@app.get("/api/v1/models")
async def list_models() -> list[dict[str, Any]]:
    return await db.get_models()


@app.post("/api/v1/models", status_code=201)
async def create_model(body: ModelCreate) -> dict[str, Any]:
    model_id = await db.add_model(body.model_dump())
    row = await db.get_model_raw(model_id)
    if row is None:
        raise HTTPException(status_code=500, detail="Failed to retrieve created model")
    return db._safe_model_row(row)


@app.delete("/api/v1/models/{model_id}")
async def delete_model(model_id: str) -> dict[str, str]:
    deleted = await db.delete_model(model_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Model not found")
    return {"ok": "deleted", "id": model_id}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Blocking entry — called by run_robber.py."""
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
