//! Native tool engine: jailed process runner, atomic diffs, SSOT artifacts.

pub mod atomic_fs;
pub mod diff_patcher;
pub mod execution_logger;
pub mod sandbox;

use std::path::{Component, Path, PathBuf};
use std::sync::mpsc::Sender;
use std::time::Duration;

use serde::Serialize;

use crate::crawl::SECRET_NAMES;
use crate::mmap_search::SearchHit;
use crate::Indexer;

pub use diff_patcher::{apply_diff_in_memory, AtomicDiffPatcher, PatchReport};
pub use crate::symbol_search::{find_symbol_references, replace_ast_node, search_workspace_symbols};
pub use execution_logger::ExecutionLogger;
pub use sandbox::{
    runaway_alloc, run_test_suite as run_test_suite_inner, CommandSpec, SandboxLimits,
    SandboxedRunner, StreamChunk, TestResult, ToolResult, DEFAULT_MEMORY_CAP, DEFAULT_TEST_TIMEOUT,
    DEFAULT_TIMEOUT,
};

pub const RULE_FILES: &[&str] = &[
    ".robber/rules.md",
    "AGENTS.md",
    "AGENT.md",
    ".cursorrules",
];

#[derive(Clone, Debug, Serialize)]
pub struct ToolRunReport {
    #[serde(flatten)]
    pub result: ToolResult,
    pub artifact_id: String,
    pub rules_hash: String,
}

pub fn confined_join(root: &Path, rel: &str) -> Result<PathBuf, String> {
    let rel = rel.trim().replace('\\', "/");
    if rel.is_empty() || rel == "." || rel == "./" {
        return Ok(root.to_path_buf());
    }
    if rel.contains('\0') {
        return Err("path contains nul".into());
    }
    let path = Path::new(&rel);
    if path.is_absolute() {
        return Err("absolute paths are not allowed".into());
    }
    for c in path.components() {
        match c {
            Component::ParentDir => return Err("path escapes workspace".into()),
            Component::Prefix(_) | Component::RootDir => {
                return Err("absolute paths are not allowed".into())
            }
            _ => {}
        }
    }
    let name = path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("");
    if SECRET_NAMES.contains(&name) {
        return Err(format!("refusing protected file: {rel}"));
    }
    Ok(root.join(path))
}

pub fn load_workspace_rules(root: &Path) -> String {
    let mut out = String::new();
    for rel in RULE_FILES {
        let path = root.join(rel);
        let Ok(text) = std::fs::read_to_string(&path) else {
            continue;
        };
        if text.is_empty() {
            continue;
        }
        let clip = if text.len() > 8 * 1024 {
            &text[..8 * 1024]
        } else {
            &text
        };
        if !out.is_empty() {
            out.push('\n');
        }
        out.push_str("# ");
        out.push_str(rel);
        out.push('\n');
        out.push_str(clip);
        if out.len() > 24 * 1024 {
            out.truncate(24 * 1024);
            break;
        }
    }
    out
}

pub fn run_tool_sandboxed(
    root: &Path,
    program: &str,
    args: &[String],
    cwd: Option<&Path>,
    timeout: Duration,
    stream: Option<Sender<StreamChunk>>,
    ssot_db: Option<&Path>,
) -> Result<ToolRunReport, String> {
    let rules = load_workspace_rules(root);
    let rules_hash = crate::hashutil::sha3_hex_str(&rules);
    let runner = SandboxedRunner::new(root).with_limits(SandboxLimits {
        memory_bytes: DEFAULT_MEMORY_CAP,
        timeout,
        stdout_max: 1024 * 1024,
    });
    let spec = CommandSpec {
        program: PathBuf::from(program),
        args: args.to_vec(),
        cwd: cwd.unwrap_or(root).to_path_buf(),
        shell: false,
    };
    let result = runner.run(spec, stream)?;
    let mut artifact_id = String::new();
    if let Some(db) = ssot_db {
        let mut log = ExecutionLogger::connect(db, root)?;
        artifact_id = log.log_tool(&result, program, &rules_hash)?;
    }
    Ok(ToolRunReport {
        result,
        artifact_id,
        rules_hash,
    })
}

/// ctypes / PyO3 stand-in: `run_test_suite(cmd, working_dir, timeout_secs) -> TestResult`.
/// Child processes stay under `DEFAULT_MEMORY_CAP` (250 MB).
pub fn run_test_suite(
    root: &Path,
    cmd: &str,
    working_dir: &str,
    timeout_secs: u64,
    ssot_db: Option<&Path>,
) -> Result<TestResult, String> {
    let mut packed = run_test_suite_inner(root, cmd, working_dir, timeout_secs)?;
    if let Some(db) = ssot_db {
        let tool = ToolResult {
            exit_code: packed.exit_code,
            stdout: packed.stdout.clone(),
            stderr: packed.stderr.clone(),
            elapsed_ms: packed.elapsed_ms,
            killed: packed.killed,
            timed_out: packed.timed_out,
            memory_exceeded: packed.memory_exceeded,
            kill_reason: packed.kill_reason.clone(),
        };
        let mut log = ExecutionLogger::connect(db, root)?;
        packed.artifact_id = log.log_tool(&tool, "test_suite", "")?;
    }
    Ok(packed)
}

pub fn apply_diff_atomic(
    root: &Path,
    diff: &str,
    ssot_db: Option<&Path>,
) -> Result<PatchReport, String> {
    let patcher = AtomicDiffPatcher::new(root);
    let mut report = patcher.apply(diff)?;
    if let Some(db) = ssot_db {
        let mut log = ExecutionLogger::connect(db, root)?;
        report.artifact_id = log.log_patch(&report)?;
    }
    Ok(report)
}

pub fn quick_search(root: &Path, pattern: &str, max_hits: usize) -> Result<Vec<SearchHit>, String> {
    Indexer::new(root).search(pattern, max_hits)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_parent_and_secrets() {
        let root = Path::new("C:/ws");
        assert!(confined_join(root, "../etc/passwd").is_err());
        assert!(confined_join(root, ".env").is_err());
        assert!(confined_join(root, "src/a.py").is_ok());
    }
}
