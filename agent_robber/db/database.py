"""Async SQLite database layer for Agent Robber.

Stores models (with encrypted API keys), secrets, and request logs.
Uses aiosqlite for async access and Fernet for envelope encryption.
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
    """Async SQLite manager. Creates schema on first connect."""

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
        """Return model dict with decrypted api_key (for internal proxy use only)."""
        row = await self.get_model_raw(model_id)
        if row is None:
            return None
        d = dict(row)
        d["api_key"] = self.vault.decrypt(d.pop("encrypted_api_key", ""))
        d["is_active"] = bool(d.get("is_active", 0))
        return d

    async def delete_model(self, model_id: str) -> bool:
        before = await self.get_model_raw(model_id)
        if before is None:
            return False
        await self.execute("DELETE FROM models WHERE id = ?", (model_id,))
        return True

    def _safe_model_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Return model dict with MASKED key for API responses."""
        d = dict(row)
        raw_key = self.vault.decrypt(d.pop("encrypted_api_key", ""))
        d["api_key"] = Vault.mask(raw_key)
        d["api_key_ready"] = bool(raw_key)
        d["is_active"] = bool(d.get("is_active", 0))
        return d

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
