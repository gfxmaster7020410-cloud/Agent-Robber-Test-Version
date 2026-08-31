"""Async SQLite database layer for Agent Robber.

Stores models (with encrypted API keys), providers, tasks, subtasks,
secrets, and request logs. Uses aiosqlite for async access and
Fernet for envelope encryption.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS models (
    id                  TEXT PRIMARY KEY,
    alias               TEXT UNIQUE NOT NULL,
    provider            TEXT NOT NULL,
    model_string        TEXT NOT NULL,
    base_url            TEXT NOT NULL DEFAULT '',
    encrypted_api_key   TEXT NOT NULL DEFAULT '',
    protocol            TEXT NOT NULL DEFAULT 'openai',
    priority            INTEGER NOT NULL DEFAULT 1,
    weight              INTEGER NOT NULL DEFAULT 50,
    is_active           BOOLEAN NOT NULL DEFAULT 1,
    health_status       TEXT NOT NULL DEFAULT 'UNKNOWN',
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS providers (
    id                  TEXT PRIMARY KEY,
    preset_name         TEXT NOT NULL,
    category            TEXT NOT NULL DEFAULT 'cloud',
    default_base_url    TEXT NOT NULL DEFAULT '',
    auth_type           TEXT NOT NULL DEFAULT 'bearer'
);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    prompt          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS subtasks (
    id                  TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL,
    title               TEXT NOT NULL,
    assigned_model_alias TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'QUEUED',
    result_summary      TEXT NOT NULL DEFAULT '',
    order_index         INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS secrets (
    id              TEXT PRIMARY KEY,
    secret_ref      TEXT UNIQUE NOT NULL,
    encrypted_val   TEXT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    target_alias    TEXT NOT NULL DEFAULT '',
    status_code     INTEGER NOT NULL DEFAULT 0,
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    tokens_used     INTEGER NOT NULL DEFAULT 0,
    error_msg       TEXT NOT NULL DEFAULT ''
);
"""

DEFAULT_PROVIDERS: list[dict[str, str]] = [
    {
        "id": "openai",
        "preset_name": "OpenAI",
        "category": "cloud",
        "default_base_url": "https://api.openai.com/v1",
        "auth_type": "bearer",
    },
    {
        "id": "anthropic",
        "preset_name": "Anthropic",
        "category": "cloud",
        "default_base_url": "https://api.anthropic.com",
        "auth_type": "x-api-key",
    },
    {
        "id": "deepseek",
        "preset_name": "DeepSeek",
        "category": "cloud",
        "default_base_url": "https://api.deepseek.com/v1",
        "auth_type": "bearer",
    },
    {
        "id": "ollama",
        "preset_name": "Ollama",
        "category": "local",
        "default_base_url": "http://localhost:11434/v1",
        "auth_type": "none",
    },
    {
        "id": "groq",
        "preset_name": "Groq",
        "category": "cloud",
        "default_base_url": "https://api.groq.com/openai/v1",
        "auth_type": "bearer",
    },
]


# ---------------------------------------------------------------------------
# Vault — Fernet key management
# ---------------------------------------------------------------------------

class Vault:
    """Manages a Fernet instance backed by a persistent key file."""

    def __init__(self, key_path: Path | None = None) -> None:
        self._key_path = key_path or (Path.cwd() / ".robber" / "vault.key")
        self._fernet: Fernet | None = None

    def _load_or_create(self) -> Fernet:
        if self._fernet is not None:
            return self._fernet
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        if self._key_path.exists():
            raw = self._key_path.read_bytes().strip()
        else:
            raw = Fernet.generate_key()
            self._key_path.write_bytes(raw)
        self._fernet = Fernet(raw)
        return self._fernet

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        return self._load_or_create().encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            return ""
        try:
            return self._load_or_create().decrypt(ciphertext.encode()).decode()
        except (InvalidToken, ValueError, TypeError):
            return ""

    @staticmethod
    def mask(plaintext: str) -> str:
        if not plaintext:
            return ""
        if len(plaintext) <= 8:
            return plaintext[:2] + "***" if len(plaintext) > 2 else "***"
        return plaintext[:7] + "..." + plaintext[-3:]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class Database:
    """Async SQLite manager. Creates schema and seeds providers on first connect."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or (Path.cwd() / ".robber" / "agent_robber.db")
        self.vault = Vault()
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._db is not None

    async def connect(self) -> aiosqlite.Connection:
        if self._db is not None:
            return self._db
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._init_schema()
        return self._db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _require(self) -> aiosqlite.Connection:
        if self._db is None:
            return await self.connect()
        return self._db

    async def _init_schema(self) -> None:
        db = await self._require()
        await db.executescript(SCHEMA_SQL)
        await db.commit()
        await self._seed_providers()

    async def _seed_providers(self) -> None:
        db = await self._require()
        async with db.execute("SELECT COUNT(*) FROM providers") as cur:
            count = (await cur.fetchone())[0]
        if count > 0:
            return
        for p in DEFAULT_PROVIDERS:
            await db.execute(
                "INSERT OR IGNORE INTO providers (id, preset_name, category, default_base_url, auth_type) "
                "VALUES (?, ?, ?, ?, ?)",
                (p["id"], p["preset_name"], p["category"], p["default_base_url"], p["auth_type"]),
            )
        await db.commit()

    # -- generic helpers ----------------------------------------------------

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        db = await self._require()
        async with self._lock:
            await db.execute(sql, params)
            await db.commit()

    async def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        db = await self._require()
        async with self._lock:
            async with db.execute(sql, params) as cur:
                row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        db = await self._require()
        async with self._lock:
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    @asynccontextmanager
    async def cursor(self) -> AsyncIterator[aiosqlite.Cursor]:
        db = await self._require()
        async with self._lock:
            cur = await db.cursor()
            try:
                yield cur
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            finally:
                await cur.close()

    # -- providers ----------------------------------------------------------

    async def get_providers(self) -> list[dict[str, Any]]:
        return await self.fetchall(
            "SELECT * FROM providers ORDER BY category, preset_name"
        )

    async def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        return await self.fetchone(
            "SELECT * FROM providers WHERE id = ?", (provider_id,)
        )

    async def get_provider_default_url(self, provider_id: str) -> str:
        row = await self.get_provider(provider_id)
        return str(row["default_base_url"]) if row else ""

    # -- models CRUD --------------------------------------------------------

    async def add_model(self, data: dict[str, Any]) -> str:
        entry_id = str(data.get("id") or uuid.uuid4())
        alias = str(data.get("alias") or "").strip()
        if not alias:
            raise ValueError("alias is required")
        provider = str(data.get("provider") or "").strip()
        if not provider:
            raise ValueError("provider is required")
        model_string = str(data.get("model_string") or "").strip()
        if not model_string:
            raise ValueError("model_string is required")

        encrypted_key = self.vault.encrypt(str(data.get("api_key") or ""))
        now = datetime.now(timezone.utc).isoformat()

        await self.execute(
            """INSERT INTO models
               (id, alias, provider, model_string, base_url, encrypted_api_key,
                protocol, priority, weight, is_active, health_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'UNKNOWN', ?)""",
            (
                entry_id,
                alias,
                provider,
                model_string,
                str(data.get("base_url") or ""),
                encrypted_key,
                str(data.get("protocol") or "openai"),
                int(data.get("priority") or 1),
                int(data.get("weight") or 50),
                1 if data.get("is_active", True) else 0,
                now,
            ),
        )
        return entry_id

    async def get_models(self, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM models"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY priority ASC, alias COLLATE NOCASE"
        rows = await self.fetchall(sql)
        return [self._safe_model_row(r) for r in rows]

    async def get_model_raw(self, model_id: str) -> dict[str, Any] | None:
        return await self.fetchone("SELECT * FROM models WHERE id = ?", (model_id,))

    async def get_model_decrypted(self, model_id: str) -> dict[str, Any] | None:
        row = await self.get_model_raw(model_id)
        if row is None:
            return None
        d = dict(row)
        d["api_key"] = self.vault.decrypt(d.pop("encrypted_api_key", ""))
        d["is_active"] = bool(d.get("is_active", 0))
        return d

    async def update_model_health(self, model_id: str, health_status: str) -> None:
        await self.execute(
            "UPDATE models SET health_status = ? WHERE id = ?",
            (health_status, model_id),
        )

    async def delete_model(self, model_id: str) -> bool:
        before = await self.get_model_raw(model_id)
        if before is None:
            return False
        await self.execute("DELETE FROM models WHERE id = ?", (model_id,))
        return True

    def _safe_model_row(self, row: dict[str, Any]) -> dict[str, Any]:
        d = dict(row)
        raw_key = self.vault.decrypt(d.pop("encrypted_api_key", ""))
        d["api_key"] = Vault.mask(raw_key)
        d["api_key_ready"] = bool(raw_key)
        d["is_active"] = bool(d.get("is_active", 0))
        return d

    # -- tasks & subtasks ---------------------------------------------------

    async def create_task(self, title: str, prompt: str) -> str:
        task_id = str(uuid.uuid4())
        await self.execute(
            """INSERT INTO tasks (id, title, prompt, status, created_at)
               VALUES (?, ?, ?, 'IN_PROGRESS', ?)""",
            (task_id, title, prompt, datetime.now(timezone.utc).isoformat()),
        )
        return task_id

    async def update_task_status(self, task_id: str, status: str) -> None:
        await self.execute(
            "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id)
        )

    async def get_latest_task(self) -> dict[str, Any] | None:
        return await self.fetchone(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT 1"
        )

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        return await self.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))

    async def add_subtask(
        self,
        task_id: str,
        title: str,
        assigned_model_alias: str,
        order_index: int,
    ) -> str:
        subtask_id = str(uuid.uuid4())
        await self.execute(
            """INSERT INTO subtasks (id, task_id, title, assigned_model_alias, status, result_summary, order_index)
               VALUES (?, ?, ?, ?, 'QUEUED', '', ?)""",
            (subtask_id, task_id, title, assigned_model_alias, order_index),
        )
        return subtask_id

    async def update_subtask_status(self, subtask_id: str, status: str, result: str = "") -> None:
        await self.execute(
            "UPDATE subtasks SET status = ?, result_summary = ? WHERE id = ?",
            (status, result, subtask_id),
        )

    async def get_subtasks(self, task_id: str) -> list[dict[str, Any]]:
        return await self.fetchall(
            "SELECT * FROM subtasks WHERE task_id = ? ORDER BY order_index ASC",
            (task_id,),
        )

    async def get_latest_task_with_subtasks(self) -> dict[str, Any] | None:
        task = await self.get_latest_task()
        if task is None:
            return None
        task["subtasks"] = await self.get_subtasks(task["id"])
        return task

    # -- secrets ------------------------------------------------------------

    async def add_secret(self, secret_ref: str, value: str) -> str:
        entry_id = str(uuid.uuid4())
        encrypted = self.vault.encrypt(value)
        await self.execute(
            "INSERT INTO secrets (id, secret_ref, encrypted_val, created_at) VALUES (?, ?, ?, ?)",
            (entry_id, secret_ref, encrypted, datetime.now(timezone.utc).isoformat()),
        )
        return entry_id

    async def get_secret(self, secret_ref: str) -> str | None:
        row = await self.fetchone(
            "SELECT encrypted_val FROM secrets WHERE secret_ref = ?", (secret_ref,)
        )
        if row is None:
            return None
        return self.vault.decrypt(row["encrypted_val"])

    # -- logs ---------------------------------------------------------------

    async def write_log(
        self,
        target_alias: str,
        status_code: int,
        latency_ms: int,
        tokens_used: int = 0,
        error_msg: str = "",
    ) -> None:
        await self.execute(
            """INSERT INTO logs (target_alias, status_code, latency_ms, tokens_used, error_msg)
               VALUES (?, ?, ?, ?, ?)""",
            (target_alias, status_code, latency_ms, tokens_used, error_msg),
        )

    async def get_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self.fetchall(
            "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
        )
