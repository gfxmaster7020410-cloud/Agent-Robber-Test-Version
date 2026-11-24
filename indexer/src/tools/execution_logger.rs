//! Persist tool and patch outcomes into SSOT `execution_artifacts`.

use std::path::Path;

use crate::hashutil::sha3_hex_str;
use crate::ssot_sync::SsotSync;

use super::diff_patcher::PatchReport;
use super::sandbox::ToolResult;

const BODY_CAP: usize = 64 * 1024;

pub struct ExecutionLogger {
    sync: SsotSync,
    project_id: String,
}

impl ExecutionLogger {
    pub fn connect(db: &Path, root: &Path) -> Result<Self, String> {
        let sync = SsotSync::open(db)?;
        let name = root
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("workspace");
        let project_id = sync.ensure_project(root, name)?;
        Ok(Self { sync, project_id })
    }

    pub fn project_id(&self) -> &str {
        &self.project_id
    }

    pub fn count(&self) -> Result<i64, String> {
        self.sync.count_artifacts(&self.project_id)
    }

    pub fn log_tool(
        &mut self,
        result: &ToolResult,
        name: &str,
        rules_hash: &str,
    ) -> Result<String, String> {
        let payload = serde_json::json!({
            "stdout": clip(&result.stdout),
            "stderr": clip(&result.stderr),
            "elapsed_ms": result.elapsed_ms,
            "killed": result.killed,
            "timed_out": result.timed_out,
            "memory_exceeded": result.memory_exceeded,
            "kill_reason": result.kill_reason,
            "rules_hash": rules_hash,
        });
        self.sync.add_artifact(
            &self.project_id,
            None,
            "tool",
            name,
            &payload.to_string(),
            result.exit_code,
        )
    }

    pub fn log_patch(&mut self, report: &PatchReport) -> Result<String, String> {
        let payload = serde_json::json!({
            "ok": report.ok,
            "files": report.files,
            "hunks": report.hunks,
            "bytes_delta": report.bytes_delta,
            "rolled_back": report.rolled_back,
            "error": report.error,
            "elapsed_ms": report.elapsed_ms,
            "checksum": sha3_hex_str(&report.files.join("|")),
        });
        self.sync.add_artifact(
            &self.project_id,
            None,
            "patch",
            "apply_diff_atomic",
            &payload.to_string(),
            if report.ok { Some(0) } else { Some(1) },
        )
    }
}

fn clip(text: &str) -> String {
    let clean = text.replace('\0', "");
    if clean.len() <= BODY_CAP {
        return clean;
    }
    let mut end = BODY_CAP;
    while end > 0 && !clean.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}\n[truncated]", &clean[..end])
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn writes_execution_artifacts_transactionally() {
        let n = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("robber-art-{n}"));
        fs::create_dir_all(&dir).unwrap();
        let db = dir.join("ssot.db");
        let mut log = ExecutionLogger::connect(&db, &dir).unwrap();
        let result = ToolResult {
            exit_code: Some(0),
            stdout: "ok".into(),
            elapsed_ms: 3,
            ..ToolResult::default()
        };
        let id = log.log_tool(&result, "echo", "abc").unwrap();
        assert_eq!(id.len(), 16);
        assert_eq!(log.count().unwrap(), 1);
        fs::remove_dir_all(&dir).ok();
    }
}
