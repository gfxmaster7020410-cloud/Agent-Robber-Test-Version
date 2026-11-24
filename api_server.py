"""
Agent Robber — MVP-v2 API Server
SQLite-backed model store, encrypted API keys, priority fallback proxy, telemetry.
Run: python api_server.py
"""

import hashlib
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("robber-api")

# ---------------------------------------------------------------------------
# Encryption vault
# ---------------------------------------------------------------------------
_VAULT_KEY_FILE = Path(__file__).parent / ".robber" / "vault.key"
_FERNET: Fernet | None = None


def _get_fernet() -> Fernet:
    global _FERNET
    if _FERNET is not None:
        return _FERNET
    _VAULT_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _VAULT_KEY_FILE.exists():
        raw = _VAULT_KEY_FILE.read_bytes().strip()
    else:
        raw = Fernet.generate_key()
        _VAULT_KEY_FILE.write_bytes(raw)
        log.info("Generated new vault key at %s", _VAULT_KEY_FILE)
    _FERNET = Fernet(raw)
    return _FERNET


def encrypt_key(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_key(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except Exception:
        return ""


def mask_key(plaintext: str) -> str:
    """Return a masked version of an API key for display (never the real value)."""
    if not plaintext:
        return ""
    if len(plaintext) <= 8:
        return plaintext[:2] + "..." + plaintext[-1:] if len(plaintext) > 3 else "***"
    return plaintext[:7] + "..." + plaintext[-3:]


def key_fingerprint(ciphertext: str) -> str:
    """SHA-256 fingerprint of the encrypted blob for audit trails."""
    return hashlib.sha256(ciphertext.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).parent / ".robber" / "agent_robber.db"


def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS models (
                id              TEXT PRIMARY KEY,
                alias           TEXT NOT NULL,
                provider        TEXT NOT NULL,
                model_id        TEXT NOT NULL,
                base_url        TEXT NOT NULL DEFAULT '',
                encrypted_api_key TEXT NOT NULL DEFAULT '',
                protocol        TEXT NOT NULL DEFAULT 'openai',
                context_length  INTEGER NOT NULL DEFAULT 128000,
                priority        INTEGER NOT NULL DEFAULT 1,
                weight          INTEGER NOT NULL DEFAULT 50,
                is_active       INTEGER NOT NULL DEFAULT 1,
                health_status   TEXT NOT NULL DEFAULT 'UNKNOWN',
                last_tested_at  TEXT,
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS logs (
                id          TEXT PRIMARY KEY,
                timestamp   TEXT NOT NULL,
                model_id    TEXT NOT NULL,
                model_alias TEXT NOT NULL,
                status_code INTEGER NOT NULL DEFAULT 0,
                latency_ms  INTEGER NOT NULL DEFAULT 0,
                tokens_used INTEGER NOT NULL DEFAULT 0,
                error_message TEXT NOT NULL DEFAULT '',
                endpoint    TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                tokens INTEGER NOT NULL DEFAULT 0,
                timestamp TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_conversation_history_session
                ON conversation_history (session_id, timestamp, id);

            CREATE TABLE IF NOT EXISTS ssot_context (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                window_tokens BIGINT NOT NULL,
                packed_tokens BIGINT NOT NULL DEFAULT 0,
                hit_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
        """)
        log.info("Database ready at %s", DB_PATH)


@contextmanager
def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Agent Robber API", version="2.0.0")


def _cors_origins() -> list[str]:
    extra = os.environ.get("ROBBER_CORS_ORIGINS", "")
    origins = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:4173",
        "http://localhost:4173",
        "http://127.0.0.1:1420",
        "http://localhost:1420",
        "http://127.0.0.1:8787",
        "http://localhost:8787",
        "https://tauri.localhost",
        "http://tauri.localhost",
    ]
    for item in extra.split(","):
        host = item.strip()
        if host and host != "*" and host not in origins:
            origins.append(host)
    return origins


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    _init_db()


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------

def _write_log(
    *,
    model_id: str,
    model_alias: str,
    status_code: int,
    latency_ms: int,
    tokens_used: int = 0,
    error_message: str = "",
    endpoint: str = "",
) -> None:
    try:
        with _db() as conn:
            conn.execute(
                """INSERT INTO logs (id, timestamp, model_id, model_alias, status_code,
                   latency_ms, tokens_used, error_message, endpoint)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    datetime.now(timezone.utc).isoformat(),
                    model_id,
                    model_alias,
                    status_code,
                    latency_ms,
                    tokens_used,
                    error_message,
                    endpoint,
                ),
            )
    except Exception as exc:
        log.warning("Failed to write log: %s", exc)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class SsotWindowBody(BaseModel):
    window: str | int | float | None = None
    limit: str | int | float | None = None


class ModelIn(BaseModel):
    alias: str = ""
    name: str = ""
    provider: str = "CUSTOM"
    provider_preset: str = ""
    base_url: str = ""
    api_key: str = ""
    model_id: str = ""
    model_string: str = ""
    protocol: str = "openai"
    protocol_type: str = ""
    context_length: int = 128000
    fallback_model_id: str | None = None
    priority: int = 1
    weight: int = 50
    is_active: bool = True
    temperature: float = 0.2
    cost_per_1m: float = 0.0


class ModelPatch(BaseModel):
    alias: str | None = None
    name: str | None = None
    provider: str | None = None
    provider_preset: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model_id: str | None = None
    model_string: str | None = None
    protocol: str | None = None
    protocol_type: str | None = None
    context_length: int | None = None
    fallback_model_id: str | None = None
    priority: int | None = None
    weight: int | None = None
    is_active: bool | None = None
    health_status: str | None = None
    temperature: float | None = None
    cost_per_1m: float | None = None


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    temperature: float = 0.7
    max_tokens: int | None = None
    stream: bool = False


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _model_row_to_safe(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a DB row to a safe dict matching core server.py model keys."""
    d = _row_to_dict(row)
    raw_key = decrypt_key(d.pop("encrypted_api_key", ""))
    alias = str(d.get("alias") or d.get("name") or d.get("model_id") or "")
    model_string = str(d.get("model_id") or "")
    protocol = str(d.get("protocol") or "openai")
    protocol_type = (
        "OPENAI_COMPATIBLE"
        if protocol.lower() in {"openai", "openai_compatible", "openai-compatible"}
        else "ANTHROPIC_COMPATIBLE"
        if "anthropic" in protocol.lower()
        else "NATIVE"
    )
    return {
        "id": d.get("id"),
        "name": alias,
        "alias": alias,
        "provider": d.get("provider") or "CUSTOM",
        "provider_preset": d.get("provider") or "CUSTOM",
        "protocol": protocol,
        "protocol_type": protocol_type,
        "base_url": d.get("base_url") or "",
        "model_id": model_string,
        "model_string": model_string,
        "api_key": mask_key(raw_key),
        "api_key_ready": bool(raw_key),
        "context_length": int(d.get("context_length") or 128000),
        "fallback_model_id": d.get("fallback_model_id") or None,
        "is_active": bool(d.get("is_active", 1)),
        "priority": d.get("priority", 1),
        "weight": d.get("weight", 50),
        "health_status": d.get("health_status", "UNKNOWN"),
        "last_tested_at": d.get("last_tested_at"),
        "latency_ms": int(d.get("latency_ms") or 0),
        "cost_per_1m": float(d.get("cost_per_1m") or 0.0),
        "temperature": float(d.get("temperature") or 0.2),
        "created_at": d.get("created_at"),
    }


def _model_row_for_proxy(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a DB row to a dict with decrypted api_key for proxy use only."""
    d = _row_to_dict(row)
    d["api_key"] = decrypt_key(d.pop("encrypted_api_key", ""))
    d["is_active"] = bool(d.get("is_active", 1))
    return d


# ---------------------------------------------------------------------------
# Routes — Model CRUD
# ---------------------------------------------------------------------------

@app.get("/api/v1/models")
async def list_models() -> list[dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM models ORDER BY priority ASC, created_at DESC").fetchall()
    return [_model_row_to_safe(r) for r in rows]


@app.post("/api/v1/models")
async def create_model(body: ModelIn) -> dict[str, Any]:
    model_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    enc_key = encrypt_key(body.api_key)
    model_string = body.model_string or body.model_id
    alias = body.name or body.alias or model_string
    provider = body.provider_preset or body.provider
    protocol = (body.protocol_type or body.protocol or "openai").split("_")[0].lower()
    if "anthropic" in protocol:
        protocol = "anthropic"
    elif protocol not in {"openai", "anthropic"}:
        protocol = "openai"
    if not model_string:
        raise HTTPException(status_code=400, detail="model_id / model_string is required")
    if not provider:
        raise HTTPException(status_code=400, detail="provider is required")

    with _db() as conn:
        conn.execute(
            """INSERT INTO models (id, alias, provider, model_id, base_url, encrypted_api_key,
               protocol, context_length, priority, weight, is_active, health_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'UNKNOWN', ?)""",
            (
                model_id,
                alias,
                provider,
                model_string,
                body.base_url,
                enc_key,
                protocol,
                body.context_length,
                body.priority,
                body.weight,
                1 if body.is_active else 0,
                now,
            ),
        )

    with _db() as conn:
        row = conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()

    log.info("Created model %s (%s)", alias, provider)
    return _model_row_to_safe(row)


@app.patch("/api/v1/models/{model_id}")
async def patch_model(model_id: str, body: ModelPatch) -> dict[str, Any]:
    updates: list[str] = []
    values: list[Any] = []

    for field in ("alias", "provider", "model_id", "base_url", "protocol", "health_status"):
        val = getattr(body, field, None)
        if val is not None:
            updates.append(f"{field} = ?")
            values.append(val)

    if body.api_key is not None:
        updates.append("encrypted_api_key = ?")
        values.append(encrypt_key(body.api_key))

    if body.context_length is not None:
        updates.append("context_length = ?")
        values.append(body.context_length)

    if body.priority is not None:
        updates.append("priority = ?")
        values.append(body.priority)

    if body.weight is not None:
        updates.append("weight = ?")
        values.append(body.weight)

    if body.is_active is not None:
        updates.append("is_active = ?")
        values.append(1 if body.is_active else 0)

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    values.append(model_id)

    with _db() as conn:
        cur = conn.execute(f"UPDATE models SET {', '.join(updates)} WHERE id = ?", values)
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Model not found")
        row = conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()

    return _model_row_to_safe(row)


@app.delete("/api/v1/models/{model_id}")
async def delete_model(model_id: str) -> dict[str, str]:
    with _db() as conn:
        cur = conn.execute("DELETE FROM models WHERE id = ?", (model_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Model not found")
    return {"ok": "deleted"}


# ---------------------------------------------------------------------------
# Routes — Test / Ping
# ---------------------------------------------------------------------------

@app.post("/api/v1/models/{model_id}/test")
async def test_model(model_id: str) -> dict[str, Any]:
    """Send a quick ping prompt to the model, update health_status, and log."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Model not found")

    model = _model_row_for_proxy(row)
    alias = row["alias"]

    t0 = time.monotonic()
    ok = False
    error_msg = ""
    status_code = 0
    try:
        result = await _proxy_request(
            model,
            messages=[{"role": "user", "content": "Hello, reply with OK."}],
            max_tokens=32,
            temperature=0.0,
        )
        ok = True
        status_code = 200
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        error_msg = f"HTTP {status_code}"
        result = {"error": error_msg}
    except Exception as exc:
        status_code = 0
        error_msg = str(exc)
        result = {"error": error_msg}

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    health = "HEALTHY" if ok else "OFFLINE"

    with _db() as conn:
        conn.execute(
            "UPDATE models SET health_status = ?, last_tested_at = ? WHERE id = ?",
            (health, datetime.now(timezone.utc).isoformat(), model_id),
        )

    _write_log(
        model_id=model_id,
        model_alias=alias,
        status_code=status_code,
        latency_ms=elapsed_ms,
        error_message=error_msg,
        endpoint="/api/v1/models/{id}/test",
    )

    return {"ok": ok, "latency_ms": elapsed_ms, "health_status": health, "response": result, "error": error_msg or None}


# ---------------------------------------------------------------------------
# Routes — Chat Completions Proxy (with fallback)
# ---------------------------------------------------------------------------

FALLBACK_RETRY_CODES = {429, 500, 502, 503}


@app.post("/api/v1/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Proxy a chat completion request with automatic priority-based fallback."""
    messages = [msg.model_dump() for msg in body.messages]

    # Collect all active models sorted by priority (1 = highest)
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM models WHERE is_active = 1 ORDER BY priority ASC, weight DESC"
        ).fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail="No active models configured.")

    # Find the exact match first, then fall through to all active models
    primary = None
    ordered: list[sqlite3.Row] = []
    for r in rows:
        if primary is None and (
            r["model_id"] == body.model
            or r["alias"] == body.model
            or r["id"] == body.model
        ):
            primary = r
        ordered.append(r)

    # If there's a primary match, put it first in the fallback list
    if primary is not None:
        ordered = [primary] + [r for r in ordered if r["id"] != primary["id"]]

    last_error: str = ""
    last_status: int = 0

    for row in ordered:
        model = _model_row_for_proxy(row)
        if not model.get("api_key"):
            continue

        alias = row["alias"]
        t0 = time.monotonic()

        try:
            result = await _proxy_request(
                model,
                messages=messages,
                max_tokens=body.max_tokens,
                temperature=body.temperature,
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            usage = result.get("usage") if isinstance(result, dict) else {}
            tokens_used = 0
            if isinstance(usage, dict):
                tokens_used = int(usage.get("total_tokens") or usage.get("output_tokens") or 0)

            _write_log(
                model_id=row["id"],
                model_alias=alias,
                status_code=200,
                latency_ms=elapsed_ms,
                tokens_used=tokens_used,
                endpoint="/api/v1/chat/completions",
            )

            # Update health on success
            with _db() as conn:
                conn.execute(
                    "UPDATE models SET health_status = 'HEALTHY', last_tested_at = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), row["id"]),
                )

            response.headers["X-Robber-Served-By"] = alias
            response.headers["X-Robber-Priority"] = str(row["priority"])
            return result

        except httpx.HTTPStatusError as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            last_error = f"HTTP {exc.response.status_code}"
            last_status = exc.response.status_code

            _write_log(
                model_id=row["id"],
                model_alias=alias,
                status_code=exc.response.status_code,
                latency_ms=elapsed_ms,
                error_message=last_error,
                endpoint="/api/v1/chat/completions",
            )

            # Update health on failure
            health = "DEGRADED" if exc.response.status_code == 429 else "OFFLINE"
            with _db() as conn:
                conn.execute(
                    "UPDATE models SET health_status = ?, last_tested_at = ? WHERE id = ?",
                    (health, datetime.now(timezone.utc).isoformat(), row["id"]),
                )

            if exc.response.status_code in FALLBACK_RETRY_CODES:
                log.warning("Model %s failed with %s — trying next fallback", alias, last_error)
                continue
            # Non-retryable error — stop here
            raise HTTPException(
                status_code=exc.response.status_code,
                detail=f"Model '{alias}' failed: {last_error}",
            )

        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            last_error = f"Connection error: {type(exc).__name__}"

            _write_log(
                model_id=row["id"],
                model_alias=alias,
                status_code=0,
                latency_ms=elapsed_ms,
                error_message=last_error,
                endpoint="/api/v1/chat/completions",
            )

            with _db() as conn:
                conn.execute(
                    "UPDATE models SET health_status = 'OFFLINE', last_tested_at = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), row["id"]),
                )

            log.warning("Model %s timed out — trying next fallback", alias)
            continue

    raise HTTPException(
        status_code=502,
        detail=f"All active models failed. Last error: {last_error}",
    )


# ---------------------------------------------------------------------------
# Routes — Logs / Telemetry
# ---------------------------------------------------------------------------

@app.get("/api/v1/logs")
async def list_logs(limit: int = 50, model_id: str | None = None) -> list[dict[str, Any]]:
    with _db() as conn:
        if model_id:
            rows = conn.execute(
                "SELECT * FROM logs WHERE model_id = ? ORDER BY timestamp DESC LIMIT ?",
                (model_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM logs ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_log_row(r) for r in rows]


def _log_row(row: sqlite3.Row) -> dict[str, Any]:
    d = _row_to_dict(row)
    return {
        "id": d.get("id"),
        "timestamp": d.get("timestamp"),
        "model_id": d.get("model_id") or "",
        "model_alias": d.get("model_alias") or "",
        "status_code": int(d.get("status_code") or 0),
        "latency_ms": int(d.get("latency_ms") or 0),
        "tokens": int(d.get("tokens_used") or d.get("tokens") or 0),
        "tokens_used": int(d.get("tokens_used") or d.get("tokens") or 0),
        "error_message": d.get("error_message") or "",
        "endpoint": d.get("endpoint") or "",
    }


def _ssot_payload() -> dict[str, Any]:
    from robber.config import DEFAULT_CONTEXT_WINDOW
    from robber.ssot_context import window_public

    payload: dict[str, Any] = {
        "files": 0,
        "chunks": 0,
        "backend": "hash-json",
        "watching": False,
        "watch_events": 0,
        "last_watch": 0.0,
    }
    try:
        from robber.ssot import get_index

        stats = getattr(get_index(), "stats", None)
        if isinstance(stats, dict):
            payload.update(stats)
    except Exception:
        pass
    try:
        from robber.ssot_watch import get_ssot_watcher

        watcher = get_ssot_watcher()
        public = watcher.public_stats() if watcher is not None else None
        if isinstance(public, dict):
            payload.update(public)
    except Exception:
        pass
    window = DEFAULT_CONTEXT_WINDOW
    packed = 0
    hits = 0
    try:
        with _db() as conn:
            row = conn.execute("SELECT * FROM ssot_context WHERE id = 1").fetchone()
        if row is not None:
            window = int(row["window_tokens"] or window)
            packed = int(row["packed_tokens"] or 0)
            hits = int(row["hit_count"] or 0)
    except Exception:
        pass
    payload.update(window_public(window, packed=packed, hit_count=hits))
    return payload


def _local_history(session_id: str, limit: int) -> list[dict[str, Any]]:
    cap = max(1, min(int(limit or 200), 1000))
    sid = (session_id or "current_session").strip() or "current_session"
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT id, session_id, role, content, tokens, timestamp
            FROM conversation_history
            WHERE session_id = ?
            ORDER BY timestamp ASC, id ASC
            LIMIT ?
            """,
            (sid, cap),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = _row_to_dict(row)
        out.append(
            {
                "id": int(d.get("id") or 0),
                "session_id": str(d.get("session_id") or sid),
                "role": str(d.get("role") or "system"),
                "content": str(d.get("content") or ""),
                "tokens": int(d.get("tokens") or 0),
                "timestamp": str(d.get("timestamp") or ""),
            }
        )
    return out


@app.get("/api/history")
@app.get("/api/v1/history")
async def get_history(session_id: str = "current_session", limit: int = 200) -> dict[str, Any]:
    sid = (session_id or "current_session").strip() or "current_session"
    turns: list[dict[str, Any]] = []
    try:
        from robber.db.database import Database
        from robber.jail import workspace_root

        db = Database(workspace_root() / ".robber" / "accounts.db")
        await db.connect()
        try:
            turns = await db.get_conversation_history(sid, limit=limit)
        finally:
            await db.close()
    except Exception:
        turns = []
    if not turns:
        turns = _local_history(sid, limit)
    return {"session_id": sid, "turns": turns}


@app.delete("/api/history")
@app.delete("/api/v1/history")
async def clear_history(session_id: str = "current_session") -> dict[str, Any]:
    sid = (session_id or "current_session").strip() or "current_session"
    try:
        from robber.db.database import Database
        from robber.jail import workspace_root

        db = Database(workspace_root() / ".robber" / "accounts.db")
        await db.connect()
        try:
            await db.clear_conversation_history(sid)
        finally:
            await db.close()
    except Exception:
        pass
    with _db() as conn:
        conn.execute("DELETE FROM conversation_history WHERE session_id = ?", (sid,))
    return {"status": "ok", "session_id": sid, "turns": []}


@app.get("/api/ssot/window")
@app.get("/api/v1/ssot")
async def get_ssot_window() -> dict[str, Any]:
    return _ssot_payload()


@app.post("/api/ssot/window")
async def set_ssot_window(payload: SsotWindowBody) -> dict[str, Any]:
    from robber.ssot_context import parse_window

    raw = payload.window if payload.window is not None else payload.limit
    if raw is None:
        raise HTTPException(status_code=400, detail="context window is empty")
    try:
        value = parse_window(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    stamp = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO ssot_context (id, window_tokens, packed_tokens, hit_count, updated_at)
            VALUES (1, ?, 0, 0, ?)
            ON CONFLICT(id) DO UPDATE SET
                window_tokens = excluded.window_tokens,
                updated_at = excluded.updated_at
            """,
            (value, stamp),
        )
    return _ssot_payload()


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "name": "agent-robber-legacy-api",
        "version": "2.0.0",
        "ssot": _ssot_payload(),
    }


# ---------------------------------------------------------------------------
# Routes — Health
# ---------------------------------------------------------------------------

@app.get("/api/v1/health")
async def health() -> dict[str, Any]:
    with _db() as conn:
        model_count = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
        active_count = conn.execute("SELECT COUNT(*) FROM models WHERE is_active = 1").fetchone()[0]
    return {
        "status": "ok",
        "version": "2.0.0",
        "models_total": model_count,
        "models_active": active_count,
        "ssot": _ssot_payload(),
    }


# ---------------------------------------------------------------------------
# Internal proxy helpers
# ---------------------------------------------------------------------------

async def _proxy_request(
    model: dict[str, Any],
    messages: list[dict[str, str]],
    max_tokens: int | None = None,
    temperature: float = 0.7,
) -> dict[str, Any]:
    protocol = model.get("protocol", "openai")
    base_url = model.get("base_url", "").rstrip("/")
    api_key = model.get("api_key", "")
    model_id = model.get("model_id", "")

    if protocol == "anthropic":
        return await _proxy_anthropic(base_url, api_key, model_id, messages, max_tokens, temperature)
    else:
        return await _proxy_openai(base_url, model_id, messages, max_tokens, temperature, api_key)


async def _proxy_openai(
    base_url: str,
    model_id: str,
    messages: list[dict[str, str]],
    max_tokens: int | None,
    temperature: float,
    api_key: str,
) -> dict[str, Any]:
    # Normalise URL
    if "/chat/completions" in base_url:
        url = base_url
    elif "/v1" in base_url:
        url = f"{base_url}/chat/completions"
    else:
        url = f"{base_url}/v1/chat/completions"

    payload: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def _proxy_anthropic(
    base_url: str,
    api_key: str,
    model_id: str,
    messages: list[dict[str, str]],
    max_tokens: int | None,
    temperature: float,
) -> dict[str, Any]:
    url = f"{base_url}/messages"

    system_text = ""
    user_messages = []
    for msg in messages:
        if msg.get("role") == "system":
            system_text += msg.get("content", "") + "\n"
        else:
            user_messages.append(msg)

    payload: dict[str, Any] = {
        "model": model_id,
        "messages": user_messages,
        "max_tokens": max_tokens or 1024,
        "temperature": temperature,
    }
    if system_text.strip():
        payload["system"] = system_text.strip()

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        anthropic_data = resp.json()

    content_parts = []
    for block in anthropic_data.get("content", []):
        if block.get("type") == "text":
            content_parts.append(block.get("text", ""))

    return {
        "id": anthropic_data.get("id", ""),
        "object": "chat.completion",
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "\n".join(content_parts)},
                "finish_reason": anthropic_data.get("stop_reason", "stop"),
            }
        ],
        "usage": anthropic_data.get("usage", {}),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("ROBBER_LEGACY_PORT", "8766")),
    )
