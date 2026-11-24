"""Shared SQLite SSOT for Python, Rust, Go, and TypeScript workers.

This is the relational worker-state store (`.robber/ssot.db`). It does not replace:
- `robber/ssot.py` hash-json vector index (`.robber/ssot.json`)
- `robber/db/database.py` accounts/models/history (`.robber/accounts.db`)

WAL + a 4-connection pool keep writers serialized and readers concurrent on an
8GB box (8MB page cache, 64MB mmap, busy timeout 5s).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from robber.config import (
    SSOT_DB_BUSY_MS,
    SSOT_DB_CACHE_KIB,
    SSOT_DB_MMAP_BYTES,
    SSOT_DB_NAME,
    SSOT_DB_POOL_SIZE,
)
from robber.jail import workspace_root

SCHEMA_VERSION = "5"
TABLES = (
    "schema_meta",
    "project_state",
    "task_state",
    "agent_state",
    "codebase_state",
    "memory_store",
    "decisions_ledger",
    "execution_artifacts",
    "ssot_change_log",
    "credential_store",
    "git_branches",
    "token_usage",
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_state (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    root_path TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'mixed',
    config_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task_state (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    parent_id TEXT,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 0,
    queue_order INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    depends_on TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (project_id) REFERENCES project_state(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES task_state(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS agent_state (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    task_id TEXT,
    worker_id TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'python',
    phase TEXT NOT NULL DEFAULT 'idle',
    loop_status TEXT NOT NULL DEFAULT '',
    telemetry_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL,
    FOREIGN KEY (project_id) REFERENCES project_state(id) ON DELETE SET NULL,
    FOREIGN KEY (task_id) REFERENCES task_state(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS codebase_state (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    path TEXT NOT NULL,
    file_hash TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    symbol_index TEXT NOT NULL DEFAULT '',
    ast_summary TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    FOREIGN KEY (project_id) REFERENCES project_state(id) ON DELETE CASCADE,
    UNIQUE (project_id, path)
);

CREATE TABLE IF NOT EXISTS memory_store (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    kind TEXT NOT NULL DEFAULT 'chunk',
    source TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    token_est INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    FOREIGN KEY (project_id) REFERENCES project_state(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS decisions_ledger (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    task_id TEXT,
    branch TEXT NOT NULL DEFAULT 'main',
    parent_id TEXT,
    choice TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    version_tag TEXT NOT NULL DEFAULT '',
    hypothesis_delta TEXT NOT NULL DEFAULT '[]',
    confidence_score REAL NOT NULL DEFAULT 0.5,
    payload TEXT NOT NULL DEFAULT '',
    checksum TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    is_final INTEGER NOT NULL DEFAULT 0,
    is_head INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (project_id) REFERENCES project_state(id) ON DELETE CASCADE,
    FOREIGN KEY (task_id) REFERENCES task_state(id) ON DELETE SET NULL,
    FOREIGN KEY (parent_id) REFERENCES decisions_ledger(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS execution_artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    task_id TEXT,
    kind TEXT NOT NULL DEFAULT 'tool',
    name TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    created_at REAL NOT NULL,
    FOREIGN KEY (project_id) REFERENCES project_state(id) ON DELETE CASCADE,
    FOREIGN KEY (task_id) REFERENCES task_state(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_task_queue
    ON task_state (status, priority DESC, queue_order, created_at);
CREATE INDEX IF NOT EXISTS idx_task_project
    ON task_state (project_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_worker
    ON agent_state (worker_id, language);
CREATE INDEX IF NOT EXISTS idx_codebase_project
    ON codebase_state (project_id, file_hash);
CREATE INDEX IF NOT EXISTS idx_memory_project
    ON memory_store (project_id, kind, created_at);
CREATE INDEX IF NOT EXISTS idx_decisions_task
    ON decisions_ledger (task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_decisions_version
    ON decisions_ledger (project_id, version_tag, created_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_task
    ON execution_artifacts (task_id, created_at);

CREATE TABLE IF NOT EXISTS credential_store (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    kind TEXT NOT NULL DEFAULT 'api_key',
    provider TEXT NOT NULL DEFAULT '',
    nonce TEXT NOT NULL DEFAULT '',
    ciphertext TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (project_id) REFERENCES project_state(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_credential_provider
    ON credential_store (provider, kind);

CREATE TABLE IF NOT EXISTS ssot_change_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    row_id TEXT NOT NULL,
    op TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ssot_change_seq ON ssot_change_log (seq);

CREATE TABLE IF NOT EXISTS system_snapshots (
    id TEXT PRIMARY KEY,
    reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    path TEXT NOT NULL DEFAULT '',
    bytes INTEGER NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    label TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_snapshots_created ON system_snapshots (created_at);

CREATE TABLE IF NOT EXISTS security_audit_log (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    command TEXT NOT NULL,
    tier INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (project_id) REFERENCES project_state(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_security_audit_ts ON security_audit_log (created_at);

CREATE TABLE IF NOT EXISTS git_branches (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    task_id TEXT,
    branch_name TEXT NOT NULL,
    base_branch TEXT NOT NULL DEFAULT 'main',
    status TEXT NOT NULL DEFAULT 'active',
    stash_ref TEXT NOT NULL DEFAULT '',
    commit_sha TEXT NOT NULL DEFAULT '',
    pr_url TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (project_id, branch_name)
);
CREATE INDEX IF NOT EXISTS idx_git_branches_task ON git_branches (task_id, updated_at);

CREATE TABLE IF NOT EXISTS token_usage (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    worker_role TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
    timestamp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_token_usage_ts ON token_usage (timestamp);
CREATE INDEX IF NOT EXISTS idx_token_usage_provider ON token_usage (provider, timestamp);

CREATE TRIGGER IF NOT EXISTS trg_task_ai AFTER INSERT ON task_state BEGIN
    INSERT INTO ssot_change_log(table_name, row_id, op, ts)
    VALUES('task_state', NEW.id, 'insert', NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS trg_task_au AFTER UPDATE ON task_state BEGIN
    INSERT INTO ssot_change_log(table_name, row_id, op, ts)
    VALUES('task_state', NEW.id, 'update', NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS trg_agent_ai AFTER INSERT ON agent_state BEGIN
    INSERT INTO ssot_change_log(table_name, row_id, op, ts)
    VALUES('agent_state', NEW.id, 'insert', NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS trg_agent_au AFTER UPDATE ON agent_state BEGIN
    INSERT INTO ssot_change_log(table_name, row_id, op, ts)
    VALUES('agent_state', NEW.id, 'update', NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS trg_art_ai AFTER INSERT ON execution_artifacts BEGIN
    INSERT INTO ssot_change_log(table_name, row_id, op, ts)
    VALUES('execution_artifacts', NEW.id, 'insert', NEW.created_at);
END;
"""

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, SsotDB] = {}
_TLS = threading.local()

DECISION_EXTRA = (
    ("version_tag", "TEXT NOT NULL DEFAULT ''"),
    ("hypothesis_delta", "TEXT NOT NULL DEFAULT '[]'"),
    ("confidence_score", "REAL NOT NULL DEFAULT 0.5"),
    ("payload", "TEXT NOT NULL DEFAULT ''"),
    ("checksum", "TEXT NOT NULL DEFAULT ''"),
    ("summary", "TEXT NOT NULL DEFAULT ''"),
    ("is_final", "INTEGER NOT NULL DEFAULT 0"),
    ("is_head", "INTEGER NOT NULL DEFAULT 0"),
)
TASK_EXTRA = (
    ("depends_on", "TEXT NOT NULL DEFAULT '[]'"),
)


def _now() -> float:
    return time.time()


def _uid() -> str:
    return uuid.uuid4().hex[:16]


def _json(value: Any) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value if value.strip() else "{}"
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [item for item in (_row(row) for row in rows) if item is not None]


class ConnectionPool:
    """Fixed-size sqlite3 pool. One connection is exclusive to one thread at a time."""

    def __init__(
        self,
        factory: Any,
        *,
        size: int = SSOT_DB_POOL_SIZE,
        timeout: float = 15.0,
    ) -> None:
        self._factory = factory
        self.size = max(1, int(size))
        self.timeout = max(0.5, float(timeout))
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._idle: list[sqlite3.Connection] = []
        self._live = 0
        self.closed = False

    def acquire(self) -> sqlite3.Connection:
        deadline = time.time() + self.timeout
        with self._cond:
            while True:
                if self.closed:
                    raise RuntimeError("SSOT connection pool is closed")
                if self._idle:
                    return self._idle.pop()
                if self._live < self.size:
                    self._live += 1
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError("SSOT connection pool exhausted")
                self._cond.wait(remaining)
        try:
            return self._factory()
        except Exception:
            with self._cond:
                self._live = max(0, self._live - 1)
                self._cond.notify()
            raise

    def release(self, conn: sqlite3.Connection) -> None:
        with self._cond:
            if self.closed:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
                self._live = max(0, self._live - 1)
                self._cond.notify()
                return
            self._idle.append(conn)
            self._cond.notify()

    def close(self) -> None:
        with self._cond:
            self.closed = True
            idle = list(self._idle)
            self._idle.clear()
            self._live = 0
            self._cond.notify_all()
        for conn in idle:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            try:
                conn.close()
            except sqlite3.Error:
                pass


class SsotDB:
    """WAL SQLite SSOT with pooled connections and rollback-safe transactions."""

    def __init__(self, path: Path | str | None = None, *, pool_size: int = SSOT_DB_POOL_SIZE) -> None:
        dest = Path(path) if path is not None else workspace_root() / ".robber" / SSOT_DB_NAME
        if dest.suffix.lower() != ".db":
            dest = dest / SSOT_DB_NAME
        self.path = dest.resolve()
        self.pool_size = max(1, int(pool_size))
        self._pool: ConnectionPool | None = None

    @property
    def connected(self) -> bool:
        return self._pool is not None and not self._pool.closed

    def connect(self) -> "SsotDB":
        if not self.connected:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._pool = ConnectionPool(self._open, size=self.pool_size)
        with self.transaction() as conn:
            conn.executescript(SCHEMA_SQL)
            self._migrate_decisions(conn)
            self._migrate_tasks(conn)
            self._migrate_change_log(conn)
            self._migrate_snapshots(conn)
            self._migrate_token_usage(conn)
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("version", SCHEMA_VERSION),
            )
        return self

    def close(self) -> None:
        pool = self._pool
        self._pool = None
        if pool is not None:
            pool.close()

    def pragmas(self) -> dict[str, Any]:
        with self.connection() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            fks = conn.execute("PRAGMA foreign_keys").fetchone()
            cache = conn.execute("PRAGMA cache_size").fetchone()
            mmap = conn.execute("PRAGMA mmap_size").fetchone()
            busy = conn.execute("PRAGMA busy_timeout").fetchone()
        return {
            "journal_mode": str(mode[0]).lower() if mode else "",
            "foreign_keys": int(fks[0]) if fks else 0,
            "cache_size": int(cache[0]) if cache else 0,
            "mmap_size": int(mmap[0]) if mmap else 0,
            "busy_timeout": int(busy[0]) if busy else 0,
            "path": str(self.path),
            "pool_size": self.pool_size,
        }

    def snapshot(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        with self.connection() as conn:
            for name in TABLES:
                row = conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()
                counts[name] = int(row[0]) if row else 0
        info = self.pragmas()
        info["tables"] = counts
        info["version"] = SCHEMA_VERSION
        return info

    def integrity_check(self) -> str:
        with self.connection() as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "fail"

    def shrink_memory(self) -> None:
        with self.connection() as conn:
            conn.execute("PRAGMA shrink_memory")
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def wal_checkpoint(self, mode: str = "TRUNCATE") -> None:
        allowed = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}
        kind = (mode or "TRUNCATE").upper()
        if kind not in allowed:
            kind = "TRUNCATE"
        with self.connection() as conn:
            conn.execute(f"PRAGMA wal_checkpoint({kind})")

    def backup_to(self, dest: Path | str) -> Path:
        """Copy this SSOT into dest using the sqlite backup API (WAL-safe)."""
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            target.unlink()
        try:
            self.wal_checkpoint("PASSIVE")
        except sqlite3.OperationalError:
            pass
        with self.connection() as conn:
            other = sqlite3.connect(str(target))
            try:
                conn.backup(other)
            finally:
                other.close()
        return target

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        existing = getattr(_TLS, "conn", None)
        if existing is not None:
            yield existing
            return
        conn = self._require().acquire()
        try:
            yield conn
        finally:
            self._require().release(conn)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        existing = getattr(_TLS, "conn", None)
        depth = int(getattr(_TLS, "depth", 0) or 0)
        if existing is not None:
            save = f"ssot_{depth}"
            existing.execute(f"SAVEPOINT {save}")
            _TLS.depth = depth + 1
            try:
                yield existing
                existing.execute(f"RELEASE SAVEPOINT {save}")
            except Exception:
                existing.execute(f"ROLLBACK TO SAVEPOINT {save}")
                existing.execute(f"RELEASE SAVEPOINT {save}")
                raise
            finally:
                _TLS.depth = depth
            return
        conn = self._require().acquire()
        _TLS.conn = conn
        _TLS.depth = 1
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            _TLS.conn = None
            _TLS.depth = 0
            self._require().release(conn)

    def put_project(
        self,
        *,
        name: str,
        root_path: str,
        language: str = "mixed",
        config: Any = None,
        metadata: Any = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        row_id = project_id or _uid()
        stamp = _now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO project_state(id, name, root_path, language, config_json, "
                "metadata_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, root_path=excluded.root_path, "
                "language=excluded.language, config_json=excluded.config_json, "
                "metadata_json=excluded.metadata_json, updated_at=excluded.updated_at",
                (
                    row_id,
                    name or "",
                    root_path or "",
                    language or "mixed",
                    _json(config),
                    _json(metadata),
                    stamp,
                    stamp,
                ),
            )
        loaded = self.get_project(row_id)
        if loaded is None:
            raise RuntimeError("project_state upsert failed")
        return loaded

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM project_state WHERE id=?", (project_id,)).fetchone()
        return _row(row)

    def put_task(
        self,
        *,
        project_id: str,
        title: str,
        status: str = "queued",
        priority: int = 0,
        queue_order: int = 0,
        parent_id: str | None = None,
        payload: Any = None,
        depends_on: Any = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        row_id = task_id or _uid()
        stamp = _now()
        deps = _json(depends_on if depends_on is not None else [])
        if deps == "{}":
            deps = "[]"
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO task_state(id, project_id, parent_id, title, status, priority, "
                "queue_order, payload_json, depends_on, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title, status=excluded.status, "
                "priority=excluded.priority, queue_order=excluded.queue_order, "
                "payload_json=excluded.payload_json, parent_id=excluded.parent_id, "
                "depends_on=excluded.depends_on, updated_at=excluded.updated_at",
                (
                    row_id,
                    project_id,
                    parent_id,
                    title or "",
                    status or "queued",
                    int(priority),
                    int(queue_order),
                    _json(payload),
                    deps,
                    stamp,
                    stamp,
                ),
            )
        loaded = self.get_task(row_id)
        if loaded is None:
            raise RuntimeError("task_state upsert failed")
        return loaded

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM task_state WHERE id=?", (task_id,)).fetchone()
        return _row(row)

    def list_queue(self, project_id: str | None = None, *, limit: int = 64) -> list[dict[str, Any]]:
        sql = (
            "SELECT * FROM task_state WHERE status IN ('queued','running','paused') "
        )
        params: list[Any] = []
        if project_id:
            sql += "AND project_id=? "
            params.append(project_id)
        sql += "ORDER BY priority DESC, queue_order ASC, created_at ASC LIMIT ?"
        params.append(max(1, int(limit)))
        with self.connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return _rows(rows)

    def put_agent(
        self,
        *,
        worker_id: str,
        language: str = "python",
        phase: str = "idle",
        loop_status: str = "",
        project_id: str | None = None,
        task_id: str | None = None,
        telemetry: Any = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        row_id = agent_id or _uid()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO agent_state(id, project_id, task_id, worker_id, language, phase, "
                "loop_status, telemetry_json, updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id, "
                "task_id=excluded.task_id, worker_id=excluded.worker_id, "
                "language=excluded.language, phase=excluded.phase, "
                "loop_status=excluded.loop_status, telemetry_json=excluded.telemetry_json, "
                "updated_at=excluded.updated_at",
                (
                    row_id,
                    project_id,
                    task_id,
                    worker_id or "",
                    language or "python",
                    phase or "idle",
                    loop_status or "",
                    _json(telemetry),
                    _now(),
                ),
            )
        loaded = self.get_agent(row_id)
        if loaded is None:
            raise RuntimeError("agent_state upsert failed")
        return loaded

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM agent_state WHERE id=?", (agent_id,)).fetchone()
        return _row(row)

    def put_codebase(
        self,
        *,
        project_id: str,
        path: str,
        file_hash: str = "",
        language: str = "",
        symbol_index: str = "",
        ast_summary: str = "",
        size_bytes: int = 0,
        row_id: str | None = None,
    ) -> dict[str, Any]:
        stamp = _now()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM codebase_state WHERE project_id=? AND path=?",
                (project_id, path),
            ).fetchone()
            ident = (existing[0] if existing else None) or row_id or _uid()
            conn.execute(
                "INSERT INTO codebase_state(id, project_id, path, file_hash, language, "
                "symbol_index, ast_summary, size_bytes, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(project_id, path) DO UPDATE SET file_hash=excluded.file_hash, "
                "language=excluded.language, symbol_index=excluded.symbol_index, "
                "ast_summary=excluded.ast_summary, size_bytes=excluded.size_bytes, "
                "updated_at=excluded.updated_at",
                (
                    ident,
                    project_id,
                    path,
                    file_hash or "",
                    language or "",
                    symbol_index or "",
                    ast_summary or "",
                    int(size_bytes),
                    stamp,
                ),
            )
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM codebase_state WHERE project_id=? AND path=?",
                (project_id, path),
            ).fetchone()
        loaded = _row(row)
        if loaded is None:
            raise RuntimeError("codebase_state upsert failed")
        return loaded

    def list_codebase(
        self,
        project_id: str | None = None,
        *,
        path: str | None = None,
        limit: int = 64,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM codebase_state WHERE 1=1"
        params: list[Any] = []
        if project_id:
            sql += " AND project_id=?"
            params.append(project_id)
        if path:
            needle = str(path).replace("\\", "/")
            sql += " AND (path=? OR path LIKE ?)"
            params.extend([needle, f"%{needle}"])
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self.connection() as conn:
            return _rows(conn.execute(sql, tuple(params)).fetchall())

    def add_memory(
        self,
        *,
        body: str,
        kind: str = "chunk",
        source: str = "",
        project_id: str | None = None,
        token_est: int = 0,
        memory_id: str | None = None,
    ) -> dict[str, Any]:
        row_id = memory_id or _uid()
        text = body or ""
        estimate = int(token_est) if token_est else max(1, (len(text) + 3) // 4)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO memory_store(id, project_id, kind, source, body, token_est, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (row_id, project_id, kind or "chunk", source or "", text, estimate, _now()),
            )
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM memory_store WHERE id=?", (row_id,)).fetchone()
        loaded = _row(row)
        if loaded is None:
            raise RuntimeError("memory_store insert failed")
        return loaded

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM memory_store WHERE id=?", (memory_id,)).fetchone()
        return _row(row)

    def list_memory(
        self,
        project_id: str | None = None,
        *,
        kind: str | None = None,
        source: str | None = None,
        limit: int = 256,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM memory_store WHERE 1=1"
        params: list[Any] = []
        if project_id:
            sql += " AND project_id=?"
            params.append(project_id)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if source:
            sql += " AND source=?"
            params.append(source)
        sql += " ORDER BY created_at ASC LIMIT ?"
        params.append(max(1, int(limit)))
        with self.connection() as conn:
            return _rows(conn.execute(sql, tuple(params)).fetchall())

    def upsert_memory(
        self,
        *,
        body: str,
        kind: str = "chunk",
        source: str = "",
        project_id: str | None = None,
        token_est: int = 0,
        memory_id: str | None = None,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        row_id = memory_id or _uid()
        text = body or ""
        estimate = int(token_est) if token_est else max(1, (len(text) + 3) // 4)
        stamp = _now() if created_at is None else float(created_at)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO memory_store(id, project_id, kind, source, body, token_est, created_at) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id, kind=excluded.kind, "
                "source=excluded.source, body=excluded.body, token_est=excluded.token_est",
                (row_id, project_id, kind or "chunk", source or "", text, estimate, stamp),
            )
        loaded = self.get_memory(row_id)
        if loaded is None:
            raise RuntimeError("memory_store upsert failed")
        return loaded

    def delete_memory(self, memory_id: str) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM memory_store WHERE id=?", (memory_id,))
            return int(cursor.rowcount or 0) > 0

    def add_decision(
        self,
        *,
        choice: str,
        rationale: str = "",
        branch: str = "main",
        project_id: str | None = None,
        task_id: str | None = None,
        parent_id: str | None = None,
        decision_id: str | None = None,
    ) -> dict[str, Any]:
        row_id = decision_id or _uid()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO decisions_ledger(id, project_id, task_id, branch, parent_id, "
                "choice, rationale, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    row_id,
                    project_id,
                    task_id,
                    branch or "main",
                    parent_id,
                    choice or "",
                    rationale or "",
                    _now(),
                ),
            )
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM decisions_ledger WHERE id=?", (row_id,)).fetchone()
        loaded = _row(row)
        if loaded is None:
            raise RuntimeError("decisions_ledger insert failed")
        return loaded

    def add_artifact(
        self,
        *,
        name: str,
        body: str = "",
        kind: str = "tool",
        project_id: str | None = None,
        task_id: str | None = None,
        exit_code: int | None = None,
        artifact_id: str | None = None,
    ) -> dict[str, Any]:
        row_id = artifact_id or _uid()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO execution_artifacts(id, project_id, task_id, kind, name, body, "
                "exit_code, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    row_id,
                    project_id,
                    task_id,
                    kind or "tool",
                    name or "",
                    body or "",
                    exit_code,
                    _now(),
                ),
            )
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM execution_artifacts WHERE id=?", (row_id,)).fetchone()
        loaded = _row(row)
        if loaded is None:
            raise RuntimeError("execution_artifacts insert failed")
        return loaded

    def record_security_audit(
        self,
        command: str,
        tier: int,
        reason: str,
        *,
        project_id: str | None = None,
        audit_id: str | None = None,
    ) -> dict[str, Any]:
        row_id = audit_id or _uid()
        now = _now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO security_audit_log(id, project_id, command, tier, reason, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (row_id, project_id, command, int(tier), reason, now),
            )
        return {
            "id": row_id,
            "project_id": project_id,
            "command": command,
            "tier": tier,
            "reason": reason,
            "created_at": now,
        }

    def find_project_by_root(self, root_path: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM project_state WHERE root_path=? ORDER BY updated_at DESC LIMIT 1",
                (root_path,),
            ).fetchone()
        return _row(row)

    def list_tasks(
        self,
        project_id: str | None = None,
        *,
        status: str | None = None,
        limit: int = 128,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM task_state WHERE 1=1"
        params: list[Any] = []
        if project_id:
            sql += " AND project_id=?"
            params.append(project_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY priority DESC, queue_order ASC, updated_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self.connection() as conn:
            return _rows(conn.execute(sql, tuple(params)).fetchall())

    def list_agents(
        self,
        project_id: str | None = None,
        *,
        worker_id: str | None = None,
        limit: int = 128,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM agent_state WHERE 1=1"
        params: list[Any] = []
        if project_id:
            sql += " AND project_id=?"
            params.append(project_id)
        if worker_id:
            sql += " AND worker_id=?"
            params.append(worker_id)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self.connection() as conn:
            return _rows(conn.execute(sql, tuple(params)).fetchall())

    def list_artifacts(
        self,
        project_id: str | None = None,
        *,
        task_id: str | None = None,
        limit: int = 128,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM execution_artifacts WHERE 1=1"
        params: list[Any] = []
        if project_id:
            sql += " AND project_id=?"
            params.append(project_id)
        if task_id:
            sql += " AND task_id=?"
            params.append(task_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self.connection() as conn:
            return _rows(conn.execute(sql, tuple(params)).fetchall())

    def upsert_git_branch(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        branch_name: str,
        base_branch: str = "main",
        status: str = "active",
        stash_ref: str = "",
        commit_sha: str = "",
        pr_url: str = "",
    ) -> dict[str, Any]:
        row_id = _uid()
        stamp = _now()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM git_branches WHERE project_id IS ? AND branch_name=?",
                (project_id, branch_name),
            ).fetchone()
            if existing:
                row_id = str(existing["id"])
                conn.execute(
                    "UPDATE git_branches SET task_id=?, base_branch=?, status=?, stash_ref=?, "
                    "commit_sha=CASE WHEN ? != '' THEN ? ELSE commit_sha END, "
                    "pr_url=CASE WHEN ? != '' THEN ? ELSE pr_url END, updated_at=? WHERE id=?",
                    (
                        task_id,
                        base_branch,
                        status,
                        stash_ref,
                        commit_sha,
                        commit_sha,
                        pr_url,
                        pr_url,
                        stamp,
                        row_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO git_branches(id, project_id, task_id, branch_name, base_branch, "
                    "status, stash_ref, commit_sha, pr_url, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row_id,
                        project_id,
                        task_id,
                        branch_name,
                        base_branch,
                        status,
                        stash_ref,
                        commit_sha,
                        pr_url,
                        stamp,
                        stamp,
                    ),
                )
            row = conn.execute("SELECT * FROM git_branches WHERE id=?", (row_id,)).fetchone()
        packed = _row(row)
        return packed or {}

    def list_git_branches(
        self,
        project_id: str | None = None,
        *,
        status: str | None = None,
        limit: int = 64,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM git_branches WHERE 1=1"
        params: list[Any] = []
        if project_id:
            sql += " AND project_id=?"
            params.append(project_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self.connection() as conn:
            return _rows(conn.execute(sql, tuple(params)).fetchall())

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM execution_artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
        return _row(row)

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM decisions_ledger WHERE id=?", (decision_id,)).fetchone()
        return _row(row)

    def list_decisions(
        self,
        project_id: str | None = None,
        *,
        branch: str | None = None,
        graph_only: bool = False,
        limit: int = 512,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM decisions_ledger WHERE 1=1"
        params: list[Any] = []
        if project_id:
            sql += " AND project_id=?"
            params.append(project_id)
        if branch:
            sql += " AND branch=?"
            params.append(branch)
        if graph_only:
            sql += " AND (version_tag != '' OR checksum != '')"
        sql += " ORDER BY created_at ASC LIMIT ?"
        params.append(max(1, int(limit)))
        with self.connection() as conn:
            return _rows(conn.execute(sql, tuple(params)).fetchall())

    def upsert_decision(
        self,
        *,
        decision_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        branch: str = "main",
        parent_id: str | None = None,
        choice: str = "",
        rationale: str = "",
        version_tag: str = "",
        hypothesis_delta: Any = None,
        confidence_score: float = 0.5,
        payload: str = "",
        checksum: str = "",
        summary: str = "",
        is_final: int = 0,
        is_head: int = 0,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        row_id = decision_id or _uid()
        stamp = _now() if created_at is None else float(created_at)
        parent = parent_id or None
        delta = hypothesis_delta
        if not isinstance(delta, str):
            delta = json.dumps(delta if delta is not None else [], ensure_ascii=True)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO decisions_ledger(id, project_id, task_id, branch, parent_id, choice, "
                "rationale, created_at, version_tag, hypothesis_delta, confidence_score, payload, "
                "checksum, summary, is_final, is_head) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id, "
                "task_id=excluded.task_id, branch=excluded.branch, parent_id=excluded.parent_id, "
                "choice=excluded.choice, rationale=excluded.rationale, "
                "version_tag=excluded.version_tag, hypothesis_delta=excluded.hypothesis_delta, "
                "confidence_score=excluded.confidence_score, payload=excluded.payload, "
                "checksum=excluded.checksum, summary=excluded.summary, "
                "is_final=excluded.is_final, is_head=excluded.is_head",
                (
                    row_id,
                    project_id,
                    task_id,
                    branch or "main",
                    parent,
                    choice or "",
                    rationale or "",
                    stamp,
                    version_tag or "",
                    delta or "[]",
                    float(confidence_score),
                    payload or "",
                    checksum or "",
                    summary or "",
                    int(is_final),
                    int(is_head),
                ),
            )
        loaded = self.get_decision(row_id)
        if loaded is None:
            raise RuntimeError("decisions_ledger upsert failed")
        return loaded

    def put_credential(
        self,
        *,
        provider: str,
        kind: str = "api_key",
        nonce: str,
        ciphertext: str,
        fingerprint: str,
        project_id: str | None = None,
        credential_id: str | None = None,
    ) -> dict[str, Any]:
        name = (provider or "").strip().lower()
        if not name:
            raise ValueError("provider is empty")
        stamp = _now()
        existing = self.find_credential(name, kind=kind)
        row_id = credential_id or (existing["id"] if existing else _uid())
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO credential_store(id, project_id, kind, provider, nonce, ciphertext, "
                "fingerprint, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id, kind=excluded.kind, "
                "provider=excluded.provider, nonce=excluded.nonce, ciphertext=excluded.ciphertext, "
                "fingerprint=excluded.fingerprint, updated_at=excluded.updated_at",
                (
                    row_id,
                    project_id,
                    kind or "api_key",
                    name,
                    nonce or "",
                    ciphertext or "",
                    fingerprint or "",
                    float(existing["created_at"]) if existing else stamp,
                    stamp,
                ),
            )
        loaded = self.get_credential(row_id)
        if loaded is None:
            raise RuntimeError("credential_store upsert failed")
        return loaded

    def get_credential(self, credential_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM credential_store WHERE id=?",
                (credential_id,),
            ).fetchone()
        return _row(row)

    def find_credential(self, provider: str, *, kind: str = "api_key") -> dict[str, Any] | None:
        name = (provider or "").strip().lower()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM credential_store WHERE provider=? AND kind=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (name, kind or "api_key"),
            ).fetchone()
        return _row(row)

    def list_credentials(self, *, kind: str | None = None, limit: int = 64) -> list[dict[str, Any]]:
        sql = "SELECT id, project_id, kind, provider, fingerprint, created_at, updated_at FROM credential_store WHERE 1=1"
        params: list[Any] = []
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY provider ASC LIMIT ?"
        params.append(max(1, int(limit)))
        with self.connection() as conn:
            return _rows(conn.execute(sql, tuple(params)).fetchall())

    def delete_credential(self, provider: str, *, kind: str = "api_key") -> int:
        name = (provider or "").strip().lower()
        with self.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM credential_store WHERE provider=? AND kind=?",
                (name, kind or "api_key"),
            )
        return int(cur.rowcount or 0)

    def get_meta(self, key: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def put_meta(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def _migrate_decisions(self, conn: sqlite3.Connection) -> None:
        have = {row[1] for row in conn.execute("PRAGMA table_info(decisions_ledger)")}
        for name, decl in DECISION_EXTRA:
            if name not in have:
                conn.execute(f"ALTER TABLE decisions_ledger ADD COLUMN {name} {decl}")

    def _migrate_tasks(self, conn: sqlite3.Connection) -> None:
        have = {row[1] for row in conn.execute("PRAGMA table_info(task_state)")}
        for name, decl in TASK_EXTRA:
            if name not in have:
                conn.execute(f"ALTER TABLE task_state ADD COLUMN {name} {decl}")

    def put_system_snapshot(
        self,
        snap_id: str,
        *,
        reason: str = "",
        path: str = "",
        bytes_total: int = 0,
        file_count: int = 0,
        label: str = "",
        created_at: float | None = None,
    ) -> None:
        stamp = float(created_at) if created_at is not None else _now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO system_snapshots(id, reason, created_at, path, bytes, file_count, label) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET reason=excluded.reason, created_at=excluded.created_at, "
                "path=excluded.path, bytes=excluded.bytes, file_count=excluded.file_count, "
                "label=excluded.label",
                (
                    snap_id,
                    reason or "",
                    stamp,
                    path or "",
                    int(bytes_total),
                    int(file_count),
                    label or "",
                ),
            )

    def list_system_snapshots(self, *, limit: int = 64) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT id, reason, created_at, path, bytes, file_count, label "
                "FROM system_snapshots ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return _rows(rows)

    def _migrate_snapshots(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS system_snapshots ("
            "id TEXT PRIMARY KEY, "
            "reason TEXT NOT NULL DEFAULT '', "
            "created_at REAL NOT NULL, "
            "path TEXT NOT NULL DEFAULT '', "
            "bytes INTEGER NOT NULL DEFAULT 0, "
            "file_count INTEGER NOT NULL DEFAULT 0, "
            "label TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_created ON system_snapshots (created_at)"
        )

    def _migrate_change_log(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ssot_change_log ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            "table_name TEXT NOT NULL, row_id TEXT NOT NULL, "
            "op TEXT NOT NULL, ts REAL NOT NULL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ssot_change_seq ON ssot_change_log (seq)")
        conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS trg_task_ai AFTER INSERT ON task_state BEGIN
                INSERT INTO ssot_change_log(table_name, row_id, op, ts)
                VALUES('task_state', NEW.id, 'insert', NEW.updated_at);
            END;
            CREATE TRIGGER IF NOT EXISTS trg_task_au AFTER UPDATE ON task_state BEGIN
                INSERT INTO ssot_change_log(table_name, row_id, op, ts)
                VALUES('task_state', NEW.id, 'update', NEW.updated_at);
            END;
            CREATE TRIGGER IF NOT EXISTS trg_agent_ai AFTER INSERT ON agent_state BEGIN
                INSERT INTO ssot_change_log(table_name, row_id, op, ts)
                VALUES('agent_state', NEW.id, 'insert', NEW.updated_at);
            END;
            CREATE TRIGGER IF NOT EXISTS trg_agent_au AFTER UPDATE ON agent_state BEGIN
                INSERT INTO ssot_change_log(table_name, row_id, op, ts)
                VALUES('agent_state', NEW.id, 'update', NEW.updated_at);
            END;
            CREATE TRIGGER IF NOT EXISTS trg_art_ai AFTER INSERT ON execution_artifacts BEGIN
                INSERT INTO ssot_change_log(table_name, row_id, op, ts)
                VALUES('execution_artifacts', NEW.id, 'insert', NEW.created_at);
            END;
            """
        )

    def list_changes(self, after: int = 0, *, limit: int = 64) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT seq, table_name, row_id, op, ts FROM ssot_change_log "
                "WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                (int(after), max(1, int(limit))),
            ).fetchall()
        return _rows(rows)

    def _migrate_token_usage(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS token_usage ("
            "id TEXT PRIMARY KEY, "
            "provider TEXT NOT NULL, "
            "model TEXT NOT NULL, "
            "worker_role TEXT NOT NULL DEFAULT '', "
            "input_tokens INTEGER NOT NULL DEFAULT 0, "
            "output_tokens INTEGER NOT NULL DEFAULT 0, "
            "estimated_cost_usd REAL NOT NULL DEFAULT 0.0, "
            "timestamp REAL NOT NULL)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_usage_ts ON token_usage (timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_usage_provider ON token_usage (provider, timestamp)"
        )

    def record_token_usage(
        self,
        *,
        provider: str,
        model: str,
        worker_role: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
        usage_id: str | None = None,
    ) -> dict[str, Any]:
        row_id = usage_id or _uid()
        stamp = _now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO token_usage(id, provider, model, worker_role, "
                "input_tokens, output_tokens, estimated_cost_usd, timestamp) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    row_id,
                    provider or "",
                    model or "",
                    worker_role or "",
                    int(input_tokens),
                    int(output_tokens),
                    float(estimated_cost_usd),
                    stamp,
                ),
            )
        return {
            "id": row_id,
            "provider": provider,
            "model": model,
            "worker_role": worker_role,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "estimated_cost_usd": float(estimated_cost_usd),
            "timestamp": stamp,
        }

    def sum_token_usage(
        self,
        *,
        since: float | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        sql = "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), " \
              "COALESCE(SUM(estimated_cost_usd),0.0), COUNT(*) FROM token_usage WHERE 1=1"
        params: list[Any] = []
        if since is not None:
            sql += " AND timestamp >= ?"
            params.append(float(since))
        if provider:
            sql += " AND provider = ?"
            params.append(provider)
        with self.connection() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        if row is None:
            return {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "count": 0}
        return {
            "input_tokens": int(row[0]),
            "output_tokens": int(row[1]),
            "cost_usd": float(row[2]),
            "count": int(row[3]),
        }

    def list_token_usage(
        self,
        *,
        since: float | None = None,
        provider: str | None = None,
        limit: int = 128,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM token_usage WHERE 1=1"
        params: list[Any] = []
        if since is not None:
            sql += " AND timestamp >= ?"
            params.append(float(since))
        if provider:
            sql += " AND provider = ?"
            params.append(provider)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self.connection() as conn:
            return _rows(conn.execute(sql, tuple(params)).fetchall())

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path),
            timeout=max(1.0, SSOT_DB_BUSY_MS / 1000.0),
            check_same_thread=False,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute(f"PRAGMA cache_size={-abs(int(SSOT_DB_CACHE_KIB))}")
        conn.execute(f"PRAGMA mmap_size={int(SSOT_DB_MMAP_BYTES)}")
        conn.execute(f"PRAGMA busy_timeout={int(SSOT_DB_BUSY_MS)}")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        return conn

    def _require(self) -> ConnectionPool:
        if self._pool is None or self._pool.closed:
            self.connect()
        assert self._pool is not None
        return self._pool


def default_ssot_path(root: Path | None = None) -> Path:
    return (root or workspace_root()).resolve() / ".robber" / SSOT_DB_NAME


def get_ssot_db(root: Path | None = None, path: Path | None = None) -> SsotDB:
    dest = (path or default_ssot_path(root)).resolve()
    key = str(dest)
    with _CACHE_LOCK:
        store = _CACHE.get(key)
        if store is None or not store.connected:
            store = SsotDB(dest)
            store.connect()
            _CACHE[key] = store
        return store


def reset_ssot_cache() -> None:
    with _CACHE_LOCK:
        stores = list(_CACHE.values())
        _CACHE.clear()
    for store in stores:
        store.close()
