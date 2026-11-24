//! Batch-write AST rows into shared SQLite `codebase_state` (WAL).

use std::collections::HashMap;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection, OptionalExtension};

use crate::ast_parser::FileAst;
use crate::hashutil::row_id;

static ART_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);

const SCHEMA_SQL: &str = r#"
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
CREATE INDEX IF NOT EXISTS idx_codebase_project
    ON codebase_state (project_id, file_hash);
CREATE TABLE IF NOT EXISTS agent_state (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    task_id TEXT,
    worker_id TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'python',
    phase TEXT NOT NULL DEFAULT 'idle',
    loop_status TEXT NOT NULL DEFAULT '',
    telemetry_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL
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
    FOREIGN KEY (project_id) REFERENCES project_state(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task
    ON execution_artifacts (task_id, created_at);
CREATE TABLE IF NOT EXISTS ssot_change_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    row_id TEXT NOT NULL,
    op TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ssot_change_seq ON ssot_change_log (seq);
CREATE TRIGGER IF NOT EXISTS trg_art_ai AFTER INSERT ON execution_artifacts BEGIN
    INSERT INTO ssot_change_log(table_name, row_id, op, ts)
    VALUES('execution_artifacts', NEW.id, 'insert', NEW.created_at);
END;
"#;

pub struct SsotSync {
    conn: Connection,
}

impl Drop for SsotSync {
    fn drop(&mut self) {
        let _ = self.conn.execute_batch("PRAGMA shrink_memory;");
    }
}

impl SsotSync {
    pub fn open(path: &Path) -> Result<Self, String> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
        }
        let conn = Connection::open(path).map_err(|e| e.to_string())?;
        conn.busy_timeout(std::time::Duration::from_millis(5000))
            .map_err(|e| e.to_string())?;
        conn.pragma_update(None, "journal_mode", "WAL")
            .map_err(|e| e.to_string())?;
        conn.pragma_update(None, "synchronous", "NORMAL")
            .map_err(|e| e.to_string())?;
        conn.pragma_update(None, "foreign_keys", "ON")
            .map_err(|e| e.to_string())?;
        conn.pragma_update(None, "cache_size", -8192)
            .map_err(|e| e.to_string())?;
        conn.pragma_update(None, "mmap_size", 64i64 * 1024 * 1024)
            .map_err(|e| e.to_string())?;
        conn.execute_batch(SCHEMA_SQL).map_err(|e| e.to_string())?;
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('indexer', '1')
             ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [],
        )
        .map_err(|e| e.to_string())?;
        Ok(Self { conn })
    }

    pub fn ensure_project(&self, root: &Path, name: &str) -> Result<String, String> {
        let root_s = root.to_string_lossy().replace('\\', "/");
        let existing: Option<String> = self
            .conn
            .query_row(
                "SELECT id FROM project_state WHERE root_path=? ORDER BY updated_at DESC LIMIT 1",
                params![root_s],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| e.to_string())?;
        if let Some(id) = existing {
            return Ok(id);
        }
        let id = row_id("project", &root_s);
        let now = now_unix();
        self.conn
            .execute(
                "INSERT INTO project_state(id, name, root_path, language, config_json, metadata_json, created_at, updated_at)
                 VALUES(?,?,?,?,?,?,?,?)",
                params![id, name, root_s, "mixed", "{\"indexer\":true}", "{\"dal\":\"rust-indexer\"}", now, now],
            )
            .map_err(|e| e.to_string())?;
        Ok(id)
    }

    pub fn existing_hashes(&self, project_id: &str) -> Result<HashMap<String, String>, String> {
        let mut stmt = self
            .conn
            .prepare("SELECT path, file_hash FROM codebase_state WHERE project_id=?")
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map(params![project_id], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(|e| e.to_string())?;
        let mut out = HashMap::new();
        for row in rows {
            let (path, hash) = row.map_err(|e| e.to_string())?;
            out.insert(path, hash);
        }
        Ok(out)
    }

    pub fn upsert_files(&mut self, project_id: &str, files: &[FileAst]) -> Result<usize, String> {
        let tx = self.conn.transaction().map_err(|e| e.to_string())?;
        let now = now_unix();
        {
            let mut stmt = tx
                .prepare(
                    "INSERT INTO codebase_state(id, project_id, path, file_hash, language, symbol_index, ast_summary, size_bytes, updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(project_id, path) DO UPDATE SET
                        file_hash=excluded.file_hash,
                        language=excluded.language,
                        symbol_index=excluded.symbol_index,
                        ast_summary=excluded.ast_summary,
                        size_bytes=excluded.size_bytes,
                        updated_at=excluded.updated_at",
                )
                .map_err(|e| e.to_string())?;
            for file in files {
                let id = row_id(project_id, &file.path);
                stmt.execute(params![
                    id,
                    project_id,
                    file.path,
                    file.file_hash,
                    file.language,
                    file.symbol_index(),
                    file.ast_summary(),
                    file.size_bytes as i64,
                    now,
                ])
                .map_err(|e| e.to_string())?;
            }
        }
        tx.commit().map_err(|e| e.to_string())?;
        Ok(files.len())
    }

    pub fn touch_agent(&self, project_id: &str, telemetry: &str) -> Result<(), String> {
        let now = now_unix();
        self.conn
            .execute(
                "INSERT INTO agent_state(id, project_id, task_id, worker_id, language, phase, loop_status, telemetry_json, updated_at)
                 VALUES('rust-indexer', ?, NULL, 'rust-indexer', 'rust', 'index', 'running', ?, ?)
                 ON CONFLICT(id) DO UPDATE SET phase=excluded.phase, telemetry_json=excluded.telemetry_json, updated_at=excluded.updated_at, project_id=excluded.project_id",
                params![project_id, telemetry, now],
            )
            .map_err(|e| e.to_string())?;
        Ok(())
    }

    pub fn count_codebase(&self, project_id: &str) -> Result<i64, String> {
        self.conn
            .query_row(
                "SELECT COUNT(*) FROM codebase_state WHERE project_id=?",
                params![project_id],
                |r| r.get(0),
            )
            .map_err(|e| e.to_string())
    }

    pub fn add_artifact(
        &mut self,
        project_id: &str,
        task_id: Option<&str>,
        kind: &str,
        name: &str,
        body: &str,
        exit_code: Option<i32>,
    ) -> Result<String, String> {
        let now = now_unix();
        let seq = ART_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let stamp = format!("{name}:{kind}:{now}:{seq}");
        let id = row_id(project_id, &stamp);
        let tx = self.conn.transaction().map_err(|e| e.to_string())?;
        tx.execute(
            "INSERT INTO execution_artifacts(id, project_id, task_id, kind, name, body, exit_code, created_at)
             VALUES(?,?,?,?,?,?,?,?)",
            params![
                id,
                project_id,
                task_id,
                kind,
                name,
                body,
                exit_code,
                now
            ],
        )
        .map_err(|e| e.to_string())?;
        tx.commit().map_err(|e| e.to_string())?;
        Ok(id)
    }

    pub fn count_artifacts(&self, project_id: &str) -> Result<i64, String> {
        self.conn
            .query_row(
                "SELECT COUNT(*) FROM execution_artifacts WHERE project_id=?",
                params![project_id],
                |r| r.get(0),
            )
            .map_err(|e| e.to_string())
    }

    pub fn last_artifact_body(&self, project_id: &str) -> Result<Option<String>, String> {
        self.conn
            .query_row(
                "SELECT body FROM execution_artifacts WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                params![project_id],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| e.to_string())
    }
}

pub(crate) fn now_unix() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ast_parser::{AstParser, Lang};

    #[test]
    fn writes_codebase_state_wal() {
        let dir = std::env::temp_dir().join(format!(
            "robber-ssot-{}",
            std::time::SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let db_path = dir.join("ssot.db");
        let mut sync = SsotSync::open(&db_path).unwrap();
        let project = sync.ensure_project(&dir, "idx").unwrap();
        let mut parser = AstParser::new();
        let ast = parser
            .parse_source(Lang::Python, b"def hello():\n    return 1\n", "a.py")
            .unwrap();
        let n = sync.upsert_files(&project, &[ast]).unwrap();
        assert_eq!(n, 1);
        assert_eq!(sync.count_codebase(&project).unwrap(), 1);
        let mode: String = sync
            .conn
            .pragma_query_value(None, "journal_mode", |r| r.get(0))
            .unwrap();
        assert_eq!(mode.to_lowercase(), "wal");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn writes_execution_artifacts() {
        let dir = std::env::temp_dir().join(format!(
            "robber-ssot-art-{}",
            std::time::SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let mut sync = SsotSync::open(&dir.join("ssot.db")).unwrap();
        let project = sync.ensure_project(&dir, "idx").unwrap();
        let id = sync
            .add_artifact(&project, None, "tool", "echo", "{\"ok\":true}", Some(0))
            .unwrap();
        assert_eq!(id.len(), 16);
        assert_eq!(sync.count_artifacts(&project).unwrap(), 1);
        std::fs::remove_dir_all(&dir).ok();
    }
}
