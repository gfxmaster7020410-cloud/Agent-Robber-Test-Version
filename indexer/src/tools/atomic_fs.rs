//! Transactional multi-file patches: RAM staging, Tree-Sitter gate, all-or-nothing commit.

use std::path::{Path, PathBuf};
use std::time::Instant;

use crate::ast_parser::{AstParser, Lang};

use super::confined_join;
use super::diff_patcher::{
    apply_hunks, parse_unified_diff, read_source, strip_fence, PatchError, PatchReport,
};

pub struct AtomicBatchPatcher {
    root: PathBuf,
}

struct StagedFile {
    rel: String,
    dest: PathBuf,
    original: Option<Vec<u8>>,
    updated: Option<String>,
}

impl AtomicBatchPatcher {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    /// Stage every file from `diff` in memory. Write disk only if every AST gate passes.
    pub fn apply(&self, diff: &str) -> PatchReport {
        let started = Instant::now();
        match self.stage(diff) {
            Ok((staged, hunks, bytes_delta)) => match self.commit_all(&staged, hunks, bytes_delta, started)
            {
                Ok(report) => report,
                Err(err) => err.into_report(
                    staged.iter().map(|s| s.rel.clone()).collect(),
                    hunks,
                    started,
                ),
            },
            Err(err) => err.into_report(Vec::new(), 0, started),
        }
    }

    fn stage(&self, diff: &str) -> Result<(Vec<StagedFile>, usize, i64), PatchError> {
        let text = strip_fence(diff);
        let files = parse_unified_diff(&text).map_err(PatchError::Parse)?;
        if files.is_empty() {
            return Err(PatchError::Parse("diff contained no files".into()));
        }
        let mut parser = AstParser::new();
        let mut staged: Vec<StagedFile> = Vec::new();
        let mut hunks = 0usize;
        let mut bytes_delta: i64 = 0;
        for file in &files {
            hunks += file.hunks.len();
            let dest = confined_join(&self.root, &file.path).map_err(PatchError::Io)?;
            if file.is_delete {
                let original = if dest.is_file() {
                    Some(std::fs::read(&dest).map_err(|e| e.to_string()).map_err(PatchError::Io)?)
                } else {
                    None
                };
                let before = original.as_ref().map(|b| b.len() as i64).unwrap_or(0);
                bytes_delta -= before;
                staged.push(StagedFile {
                    rel: file.path.clone(),
                    dest,
                    original,
                    updated: None,
                });
                continue;
            }
            let existed = dest.is_file();
            let source = read_source(&dest).map_err(PatchError::Io)?;
            let original = if existed {
                Some(source.as_bytes().to_vec())
            } else if file.is_new {
                None
            } else {
                Some(Vec::new())
            };
            let updated = apply_hunks(&source, &file.hunks).map_err(PatchError::Hunk)?;
            if let Some(lang) = Lang::from_path(&dest) {
                if let Some(hit) = parser
                    .syntax_errors(lang, updated.as_bytes())
                    .into_iter()
                    .next()
                {
                    return Err(PatchError::ast(&file.path, &hit));
                }
            }
            bytes_delta += updated.len() as i64 - source.len() as i64;
            staged.push(StagedFile {
                rel: file.path.clone(),
                dest,
                original,
                updated: Some(updated),
            });
        }
        Ok((staged, hunks, bytes_delta))
    }

    fn commit_all(
        &self,
        staged: &[StagedFile],
        hunks: usize,
        bytes_delta: i64,
        started: Instant,
    ) -> Result<PatchReport, PatchError> {
        let names: Vec<String> = staged.iter().map(|s| s.rel.clone()).collect();
        let mut written: Vec<&StagedFile> = Vec::new();
        for item in staged {
            if let Err(e) = commit_one(item) {
                rollback(&written);
                return Err(PatchError::Io(e));
            }
            written.push(item);
        }
        let elapsed = started.elapsed();
        Ok(PatchReport {
            ok: true,
            files: names,
            hunks,
            bytes_delta,
            rolled_back: false,
            error: String::new(),
            error_kind: String::new(),
            ast_errors: Vec::new(),
            elapsed_ms: elapsed.as_millis(),
            elapsed_us: elapsed.as_micros(),
            artifact_id: String::new(),
        })
    }
}

fn commit_one(item: &StagedFile) -> Result<(), String> {
    match &item.updated {
        None => {
            if item.dest.exists() {
                std::fs::remove_file(&item.dest).map_err(|e| e.to_string())?;
            }
            Ok(())
        }
        Some(text) => {
            if let Some(parent) = item.dest.parent() {
                std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
            }
            std::fs::write(&item.dest, text.as_bytes()).map_err(|e| e.to_string())
        }
    }
}

fn rollback(written: &[&StagedFile]) {
    for item in written.iter().rev() {
        match &item.original {
            Some(bytes) => {
                let _ = std::fs::write(&item.dest, bytes);
            }
            None => {
                let _ = std::fs::remove_file(&item.dest);
            }
        }
    }
}

#[allow(dead_code)]
fn _root_as_path(p: &AtomicBatchPatcher) -> &Path {
    &p.root
}
