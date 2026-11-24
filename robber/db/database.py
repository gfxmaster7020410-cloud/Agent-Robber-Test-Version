"""Async SQLite store for accounts, cookies, logs, models, combos, and chat history."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken

from robber.jail import workspace_root

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    profile_id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    encrypted_password TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'idle',
    assigned_proxy_id TEXT,
    created_at TEXT NOT NULL,
    last_active TEXT
);

CREATE TABLE IF NOT EXISTS session_cookies (
    profile_id TEXT PRIMARY KEY,
    cookies_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES accounts(profile_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS execution_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT,
    action_name TEXT NOT NULL,
    status TEXT NOT NULL,
    log_message TEXT NOT NULL DEFAULT '',
    timestamp TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES accounts(profile_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_execution_logs_profile
    ON execution_logs (profile_id, timestamp);

CREATE TABLE IF NOT EXISTS ssot_context (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    window_tokens BIGINT NOT NULL,
    packed_tokens BIGINT NOT NULL DEFAULT 0,
    hit_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS custom_models (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    provider_type TEXT NOT NULL CHECK(provider_type IN ('NATIVE', 'CUSTOM_BASE', 'LOCAL_OLLAMA')),
    base_url TEXT DEFAULT '',
    api_key TEXT DEFAULT '',
    batch_class TEXT NOT NULL DEFAULT 'NORMAL' CHECK(batch_class IN ('ECO', 'NORMAL', 'HEAVY_20X')),
    context_length INTEGER NOT NULL DEFAULT 8192,
    fallback_model_id TEXT,
    custom_headers TEXT,
    is_active BOOLEAN NOT NULL DEFAULT 1,
    latency_ms INTEGER DEFAULT 0,
    cost_per_1m REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    provider_preset TEXT NOT NULL,
    protocol_type TEXT NOT NULL,
    base_url TEXT DEFAULT '',
    model_string TEXT DEFAULT '',
    api_key TEXT DEFAULT '',
    custom_headers TEXT DEFAULT '{}',
    context_length INTEGER DEFAULT 128000,
    fallback_model_id TEXT,
    is_active INTEGER DEFAULT 1,
    latency_ms INTEGER DEFAULT 0,
    cost_per_1m REAL DEFAULT 0.0,
    temperature REAL NOT NULL DEFAULT 0.2,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS combo_models (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tier TEXT NOT NULL,
    reasoning_model_id TEXT,
    coding_model_ids TEXT NOT NULL,
    general_model_ids TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
"""

PROTOCOL_TYPES = ("OPENAI_COMPATIBLE", "ANTHROPIC_COMPATIBLE", "NATIVE")
COMBO_TIERS = ("ECO", "BALANCED", "BEST")
MODEL_UPDATE_FIELDS = {
    "name",
    "provider_preset",
    "protocol_type",
    "base_url",
    "model_string",
    "api_key",
    "custom_headers",
    "context_length",
    "fallback_model_id",
    "is_active",
    "latency_ms",
    "cost_per_1m",
    "temperature",
}
COMBO_UPDATE_FIELDS = {
    "name",
    "tier",
    "reasoning_model_id",
    "coding_model_ids",
    "general_model_ids",
    "is_active",
}


def _load_fernet() -> Fernet:
    raw = os.environ.get("ROBBER_ACCOUNT_KEY", "").strip()
    if raw:
        return Fernet(raw.encode("utf-8"))
    path = workspace_root() / ".robber" / "account.key"
    if path.is_file():
        return Fernet(path.read_bytes().strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    path.write_bytes(key)
    return Fernet(key)


class Database:
    """Process-wide aiosqlite manager. Schema is created/migrated on connect."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (workspace_root() / ".robber" / "accounts.db")
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._fernet = _load_fernet()

    @property
    def connected(self) -> bool:
        return self._db is not None

    async def connect(self) -> aiosqlite.Connection:
        if self._db is not None:
            return self._db
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self.init_schema()
        return self._db

    async def init_schema(self) -> None:
        db = await self._require()
        await db.executescript(SCHEMA_SQL)
        await db.commit()
        await self._migrate_orchestration()
        await self._migrate_ssot_bigint()

    async def close(self) -> None:
        if self._db is None:
            return
        await self._db.close()
        self._db = None

    async def _require(self) -> aiosqlite.Connection:
        if self._db is None:
            return await self.connect()
        return self._db

    @asynccontextmanager
    async def cursor(self) -> AsyncIterator[aiosqlite.Cursor]:
        db = await self._require()
        async with self._lock:
            cursor = await db.cursor()
            try:
                yield cursor
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            finally:
                await cursor.close()

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        db = await self._require()
        async with self._lock:
            await db.execute(sql, params)
            await db.commit()

    async def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        db = await self._require()
        async with self._lock:
            async with db.execute(sql, params) as cursor:
                row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        db = await self._require()
        async with self._lock:
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    def _encrypt_secret(self, secret: str) -> str:
        value = (secret or "").strip()
        if not value:
            return ""
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def _decrypt_secret(self, stored: str) -> str:
        raw = (stored or "").strip()
        if not raw:
            return ""
        try:
            return self._fernet.decrypt(raw.encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            # Legacy plaintext from custom_models — treat as the secret itself.
            return raw

    def _headers_json(self, value: object) -> str:
        if isinstance(value, str):
            text = value.strip() or "{}"
            parsed = json.loads(text)
        elif value is None:
            parsed = {}
        else:
            parsed = value
        if not isinstance(parsed, dict):
            raise ValueError("custom_headers must be a JSON object")
        if len(parsed) > 32:
            raise ValueError("too many custom headers")
        clean: dict[str, str] = {}
        for key, item in parsed.items():
            name = str(key).strip()
            if name:
                clean[name] = str(item)
        return json.dumps(clean, separators=(",", ":"))

    def _parse_headers(self, raw: object) -> dict[str, str]:
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(key): str(value) for key, value in payload.items() if str(key).strip()}

    def _parse_id_list(self, raw: object) -> list[str]:
        if isinstance(raw, list):
            return [str(item).strip() for item in raw if str(item).strip()]
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return []
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError("model id list must be valid JSON") from exc
            if not isinstance(payload, list):
                raise ValueError("model id list must be a JSON array")
            return [str(item).strip() for item in payload if str(item).strip()]
        return []

    def _public_model(self, row: dict[str, Any]) -> dict[str, Any]:
        secret = self._decrypt_secret(str(row.get("api_key") or ""))
        return {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "provider_preset": str(row.get("provider_preset") or "CUSTOM"),
            "protocol_type": str(row.get("protocol_type") or "OPENAI_COMPATIBLE"),
            "base_url": str(row.get("base_url") or ""),
            "model_string": str(row.get("model_string") or ""),
            "custom_headers": self._parse_headers(row.get("custom_headers") or "{}"),
            "context_length": int(row.get("context_length") or 128000),
            "fallback_model_id": str(row.get("fallback_model_id") or "") or None,
            "is_active": bool(int(row.get("is_active") or 0)),
            "latency_ms": int(row.get("latency_ms") or 0),
            "cost_per_1m": float(row.get("cost_per_1m") or 0.0),
            "temperature": float(row.get("temperature") or 0.2),
            "created_at": str(row.get("created_at") or ""),
            "key_ready": bool(secret),
        }

    def _public_combo(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "tier": str(row.get("tier") or "BALANCED"),
            "reasoning_model_id": str(row.get("reasoning_model_id") or "") or None,
            "coding_model_ids": self._parse_id_list(row.get("coding_model_ids") or "[]"),
            "general_model_ids": self._parse_id_list(row.get("general_model_ids") or "[]"),
            "is_active": bool(int(row.get("is_active") or 0)),
            "created_at": str(row.get("created_at") or ""),
        }

    async def _migrate_orchestration(self) -> None:
        db = await self._require()
        info = await self.fetchall("PRAGMA table_info(models)")
        columns = {str(item.get("name") or "") for item in info}
        if "temperature" not in columns:
            try:
                await db.execute("ALTER TABLE models ADD COLUMN temperature REAL NOT NULL DEFAULT 0.2")
                await db.commit()
            except Exception:
                pass
        existing = await self.fetchone("SELECT id FROM models LIMIT 1")
        if existing is not None:
            return
        legacy = await self.fetchall("SELECT * FROM custom_models")
        for row in legacy:
            provider_type = str(row.get("provider_type") or "CUSTOM_BASE").upper()
            if provider_type == "LOCAL_OLLAMA":
                preset, protocol = "OLLAMA", "OPENAI_COMPATIBLE"
            elif provider_type == "NATIVE":
                preset, protocol = "CUSTOM", "NATIVE"
            else:
                preset, protocol = "CUSTOM", "OPENAI_COMPATIBLE"
            await self.add_model(
                {
                    "id": str(row.get("id") or uuid.uuid4()),
                    "name": str(row.get("name") or "legacy"),
                    "provider_preset": preset,
                    "protocol_type": protocol,
                    "base_url": str(row.get("base_url") or ""),
                    "model_string": str(row.get("name") or ""),
                    "api_key": self._decrypt_secret(str(row.get("api_key") or "")),
                    "custom_headers": row.get("custom_headers") or "{}",
                    "context_length": int(row.get("context_length") or 8192),
                    "fallback_model_id": row.get("fallback_model_id"),
                    "is_active": int(row.get("is_active") or 1),
                    "latency_ms": int(row.get("latency_ms") or 0),
                    "cost_per_1m": float(row.get("cost_per_1m") or 0.0),
                }
            )

    async def _migrate_ssot_bigint(self) -> None:
        """Store SSOT token budgets as 64-bit integers (SQLite BIGINT / INTEGER affinity)."""
        db = await self._require()
        info = await self.fetchall("PRAGMA table_info(ssot_context)")
        types = {
            str(item.get("name") or ""): str(item.get("type") or "").upper()
            for item in info
        }
        if not types:
            return
        if types.get("window_tokens") == "BIGINT" and types.get("packed_tokens") == "BIGINT":
            return
        try:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS ssot_context_bigint (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    window_tokens BIGINT NOT NULL,
                    packed_tokens BIGINT NOT NULL DEFAULT 0,
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                INSERT OR REPLACE INTO ssot_context_bigint
                    (id, window_tokens, packed_tokens, hit_count, updated_at)
                SELECT id, window_tokens, packed_tokens, hit_count, updated_at
                FROM ssot_context;
                DROP TABLE ssot_context;
                ALTER TABLE ssot_context_bigint RENAME TO ssot_context;
                """
            )
            await db.commit()
        except Exception:
            pass

    async def add_model(self, data: dict[str, Any]) -> str:
        from robber.engine.presets import PROTOCOL_TYPES, get_preset

        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("model name is empty")
        preset = get_preset(str(data.get("provider_preset") or "CUSTOM"))
        protocol = str(data.get("protocol_type") or preset["protocol"]).strip().upper()
        if protocol not in PROTOCOL_TYPES:
            raise ValueError("protocol_type must be OPENAI_COMPATIBLE, ANTHROPIC_COMPATIBLE, or NATIVE")
        entry_id = str(data.get("id") or "").strip() or str(uuid.uuid4())
        headers = self._headers_json(data.get("custom_headers", preset["headers"]))
        secret = str(data.get("api_key") or "").strip()
        await self.execute(
            """
            INSERT INTO models (
                id, name, provider_preset, protocol_type, base_url, model_string,
                api_key, custom_headers, context_length, fallback_model_id, is_active,
                latency_ms, cost_per_1m, temperature, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                provider_preset = excluded.provider_preset,
                protocol_type = excluded.protocol_type,
                base_url = excluded.base_url,
                model_string = excluded.model_string,
                api_key = CASE WHEN excluded.api_key = '' THEN models.api_key ELSE excluded.api_key END,
                custom_headers = excluded.custom_headers,
                context_length = excluded.context_length,
                fallback_model_id = excluded.fallback_model_id,
                is_active = excluded.is_active,
                latency_ms = excluded.latency_ms,
                cost_per_1m = excluded.cost_per_1m,
                temperature = excluded.temperature
            """,
            (
                entry_id,
                name,
                preset["id"],
                protocol,
                str(data.get("base_url") if data.get("base_url") is not None else preset["base_url"]).strip(),
                str(data.get("model_string") or preset["model_placeholder"]).strip(),
                self._encrypt_secret(secret),
                headers,
                max(1024, min(int(data.get("context_length") or 128000), 2_000_000)),
                str(data.get("fallback_model_id") or "").strip() or None,
                1 if data.get("is_active", True) not in {False, 0, "0", "false"} else 0,
                int(data.get("latency_ms") or 0),
                float(data.get("cost_per_1m") or 0.0),
                float(data.get("temperature") or 0.2),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return entry_id

    async def get_models(self, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM models"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY name COLLATE NOCASE"
        rows = await self.fetchall(sql)
        return [self._public_model(row) for row in rows]

    async def get_model_row(self, model_id: str) -> dict[str, Any] | None:
        row = await self.fetchone("SELECT * FROM models WHERE id = ?", (model_id,))
        return row

    async def get_model_secret(self, model_id: str) -> str:
        row = await self.get_model_row(model_id)
        if row is None:
            return ""
        return self._decrypt_secret(str(row.get("api_key") or ""))

    async def update_model(self, model_id: str, updates: dict[str, Any]) -> bool:
        from robber.engine.presets import PROTOCOL_TYPES, get_preset

        existing = await self.get_model_row(model_id)
        if existing is None:
            return False
        payload = dict(updates)
        if "provider_preset" in payload:
            payload["provider_preset"] = get_preset(str(payload["provider_preset"]))["id"]
        if "protocol_type" in payload:
            protocol = str(payload["protocol_type"]).strip().upper()
            if protocol not in PROTOCOL_TYPES:
                raise ValueError("protocol_type must be OPENAI_COMPATIBLE, ANTHROPIC_COMPATIBLE, or NATIVE")
            payload["protocol_type"] = protocol
        if "custom_headers" in payload:
            payload["custom_headers"] = self._headers_json(payload["custom_headers"])
        if "api_key" in payload:
            secret = str(payload.get("api_key") or "").strip()
            if not secret:
                payload.pop("api_key")
            else:
                payload["api_key"] = self._encrypt_secret(secret)
        if "is_active" in payload:
            payload["is_active"] = 1 if payload["is_active"] not in {False, 0, "0", "false"} else 0
        fields = [(key, payload[key]) for key in MODEL_UPDATE_FIELDS if key in payload]
        if not fields:
            return True
        assignments = ", ".join(f"{key} = ?" for key, _ in fields)
        values = tuple(value for _, value in fields) + (model_id,)
        await self.execute(f"UPDATE models SET {assignments} WHERE id = ?", values)
        return True

    async def delete_model(self, model_id: str) -> bool:
        before = await self.get_model_row(model_id)
        if before is None:
            return False
        await self.execute("DELETE FROM models WHERE id = ?", (model_id,))
        return True

    async def set_model_latency(self, model_id: str, latency_ms: int) -> None:
        await self.execute(
            "UPDATE models SET latency_ms = ? WHERE id = ?",
            (max(0, int(latency_ms)), model_id),
        )

    async def create_combo_model(self, combo_data: dict[str, Any]) -> str:
        from robber.engine.presets import normalize_tier

        name = str(combo_data.get("name") or "").strip()
        if not name:
            raise ValueError("combo name is empty")
        tier = normalize_tier(str(combo_data.get("tier") or "BALANCED"))
        coding = self._parse_id_list(combo_data.get("coding_model_ids") or [])
        general = self._parse_id_list(combo_data.get("general_model_ids") or [])
        combo_id = str(combo_data.get("id") or "").strip() or str(uuid.uuid4())
        await self.execute(
            """
            INSERT INTO combo_models (
                id, name, tier, reasoning_model_id, coding_model_ids,
                general_model_ids, is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                tier = excluded.tier,
                reasoning_model_id = excluded.reasoning_model_id,
                coding_model_ids = excluded.coding_model_ids,
                general_model_ids = excluded.general_model_ids,
                is_active = excluded.is_active
            """,
            (
                combo_id,
                name,
                tier,
                str(combo_data.get("reasoning_model_id") or "").strip() or None,
                json.dumps(coding),
                json.dumps(general),
                1 if combo_data.get("is_active", True) not in {False, 0, "0", "false"} else 0,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return combo_id

    async def get_combo_models(self) -> list[dict[str, Any]]:
        rows = await self.fetchall("SELECT * FROM combo_models ORDER BY tier, name COLLATE NOCASE")
        return [self._public_combo(row) for row in rows]

    async def get_combo_for_tier(self, tier: str) -> dict[str, Any] | None:
        from robber.engine.presets import normalize_tier

        wanted = normalize_tier(tier)
        row = await self.fetchone(
            "SELECT * FROM combo_models WHERE tier = ? AND is_active = 1 ORDER BY created_at DESC",
            (wanted,),
        )
        return self._public_combo(row) if row else None

    async def update_combo_model(self, combo_id: str, updates: dict[str, Any]) -> bool:
        from robber.engine.presets import normalize_tier

        existing = await self.fetchone("SELECT id FROM combo_models WHERE id = ?", (combo_id,))
        if existing is None:
            return False
        payload = dict(updates)
        if "tier" in payload:
            payload["tier"] = normalize_tier(str(payload["tier"]))
        if "coding_model_ids" in payload:
            payload["coding_model_ids"] = json.dumps(self._parse_id_list(payload["coding_model_ids"]))
        if "general_model_ids" in payload:
            payload["general_model_ids"] = json.dumps(self._parse_id_list(payload["general_model_ids"]))
        if "is_active" in payload:
            payload["is_active"] = 1 if payload["is_active"] not in {False, 0, "0", "false"} else 0
        fields = [(key, payload[key]) for key in COMBO_UPDATE_FIELDS if key in payload]
        if not fields:
            return True
        assignments = ", ".join(f"{key} = ?" for key, _ in fields)
        values = tuple(value for _, value in fields) + (combo_id,)
        await self.execute(f"UPDATE combo_models SET {assignments} WHERE id = ?", values)
        return True

    async def get_custom_models(self) -> list[dict[str, Any]]:
        return await self.get_models(active_only=True)

    async def add_custom_model(self, model_data: dict[str, Any]) -> None:
        mapped = dict(model_data)
        if "provider_type" in mapped and "provider_preset" not in mapped:
            kind = str(mapped.get("provider_type") or "").upper()
            if kind == "LOCAL_OLLAMA":
                mapped["provider_preset"] = "OLLAMA"
                mapped["protocol_type"] = "OPENAI_COMPATIBLE"
            elif kind == "NATIVE":
                mapped["protocol_type"] = "NATIVE"
        if "name" not in mapped and mapped.get("id"):
            mapped["name"] = mapped["id"]
        await self.add_model(mapped)

    async def delete_custom_model(self, model_id: str) -> None:
        await self.delete_model(model_id)

    async def update_model_status(self, model_id: str, is_active: bool) -> None:
        await self.update_model(model_id, {"is_active": is_active})

    async def get_ssot_window(self) -> int | None:
        row = await self.fetchone(
            "SELECT window_tokens, packed_tokens, hit_count FROM ssot_context WHERE id = 1"
        )
        if row is None:
            return None
        try:
            return int(row["window_tokens"])
        except (KeyError, TypeError, ValueError):
            return None

    async def get_ssot_context_row(self) -> dict[str, Any] | None:
        return await self.fetchone(
            "SELECT window_tokens, packed_tokens, hit_count, updated_at FROM ssot_context WHERE id = 1"
        )

    async def set_ssot_window(self, tokens: int) -> int:
        from robber.ssot_context import normalize_window

        value = normalize_window(int(tokens))
        stamp = datetime.now(timezone.utc).isoformat()
        await self.execute(
            """
            INSERT INTO ssot_context (id, window_tokens, packed_tokens, hit_count, updated_at)
            VALUES (1, ?, 0, 0, ?)
            ON CONFLICT(id) DO UPDATE SET
                window_tokens = excluded.window_tokens,
                updated_at = excluded.updated_at
            """,
            (value, stamp),
        )
        return value

    async def set_ssot_packed(self, packed_tokens: int, hit_count: int = 0) -> None:
        from robber.ssot_context import DEFAULT_CONTEXT_WINDOW

        stamp = datetime.now(timezone.utc).isoformat()
        packed = max(0, int(packed_tokens))
        hits = max(0, int(hit_count))
        row = await self.get_ssot_context_row()
        window = int(row["window_tokens"]) if row is not None else DEFAULT_CONTEXT_WINDOW
        await self.execute(
            """
            INSERT INTO ssot_context (id, window_tokens, packed_tokens, hit_count, updated_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                packed_tokens = excluded.packed_tokens,
                hit_count = excluded.hit_count,
                updated_at = excluded.updated_at
            """,
            (window, packed, hits, stamp),
        )

    async def add_conversation_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        tokens: int | None = None,
        timestamp: str | None = None,
    ) -> int:
        sid = (session_id or "current_session").strip() or "current_session"
        text = content or ""
        stamp = timestamp or datetime.now(timezone.utc).isoformat()
        counted = tokens if tokens is not None else max(0, (len(text) + 3) // 4)
        db = await self._require()
        async with self._lock:
            cursor = await db.execute(
                """
                INSERT INTO conversation_history (session_id, role, content, tokens, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sid, (role or "system").strip() or "system", text, counted, stamp),
            )
            await db.commit()
            return int(cursor.lastrowid or 0)

    async def get_conversation_history(
        self,
        session_id: str,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sid = (session_id or "current_session").strip() or "current_session"
        cap = max(1, min(int(limit or 200), 1000))
        rows = await self.fetchall(
            """
            SELECT id, session_id, role, content, tokens, timestamp
            FROM conversation_history
            WHERE session_id = ?
            ORDER BY timestamp ASC, id ASC
            LIMIT ?
            """,
            (sid, cap),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "id": int(row.get("id") or 0),
                    "session_id": str(row.get("session_id") or sid),
                    "role": str(row.get("role") or "system"),
                    "content": str(row.get("content") or ""),
                    "tokens": int(row.get("tokens") or 0),
                    "timestamp": str(row.get("timestamp") or ""),
                }
            )
        return out

    async def clear_conversation_history(self, session_id: str) -> None:
        sid = (session_id or "current_session").strip() or "current_session"
        await self.execute("DELETE FROM conversation_history WHERE session_id = ?", (sid,))

