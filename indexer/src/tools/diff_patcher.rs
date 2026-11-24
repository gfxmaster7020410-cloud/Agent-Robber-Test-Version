//! Atomic multi-file unified-diff apply with tree-sitter syntax gate.

use std::path::PathBuf;
use std::time::Instant;

use serde::Serialize;

use crate::ast_parser::{AstErrorHit, AstParser, Lang};

use super::confined_join;

#[derive(Clone, Debug, Default, Serialize)]
pub struct PatchReport {
    pub ok: bool,
    pub files: Vec<String>,
    pub hunks: usize,
    pub bytes_delta: i64,
    pub rolled_back: bool,
    pub error: String,
    #[serde(default)]
    pub error_kind: String,
    #[serde(default)]
    pub ast_errors: Vec<AstErrorHit>,
    pub elapsed_ms: u128,
    #[serde(default)]
    pub elapsed_us: u128,
    pub artifact_id: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct AstValidationError {
    pub path: String,
    pub line: u32,
    pub column: u32,
    pub kind: String,
    pub node_type: String,
    pub snippet: String,
    pub message: String,
}

#[derive(Clone, Debug)]
pub enum PatchError {
    Parse(String),
    Hunk(String),
    AstValidation(AstValidationError),
    Io(String),
}

impl PatchError {
    pub fn ast(path: &str, hit: &AstErrorHit) -> Self {
        PatchError::AstValidation(AstValidationError {
            path: path.to_string(),
            line: hit.line,
            column: hit.column,
            kind: hit.kind.clone(),
            node_type: hit.node_type.clone(),
            snippet: hit.snippet.clone(),
            message: format!(
                "AstValidationError {} {}:{} {} `{}`",
                hit.kind, path, hit.line, hit.node_type, hit.snippet
            ),
        })
    }

    pub fn into_report(self, files: Vec<String>, hunks: usize, started: Instant) -> PatchReport {
        let elapsed = started.elapsed();
        let (error, error_kind, ast_errors) = match &self {
            PatchError::AstValidation(err) => (
                err.message.clone(),
                "ast".to_string(),
                vec![AstErrorHit {
                    line: err.line,
                    column: err.column,
                    kind: err.kind.clone(),
                    node_type: err.node_type.clone(),
                    snippet: err.snippet.clone(),
                }],
            ),
            PatchError::Parse(e) | PatchError::Hunk(e) | PatchError::Io(e) => {
                (e.clone(), "hunk".to_string(), Vec::new())
            }
        };
        PatchReport {
            ok: false,
            files,
            hunks,
            bytes_delta: 0,
            rolled_back: true,
            error,
            error_kind,
            ast_errors,
            elapsed_ms: elapsed.as_millis(),
            elapsed_us: elapsed.as_micros(),
            artifact_id: String::new(),
        }
    }
}

impl std::fmt::Display for PatchError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PatchError::AstValidation(e) => write!(f, "{}", e.message),
            PatchError::Parse(e) | PatchError::Hunk(e) | PatchError::Io(e) => write!(f, "{e}"),
        }
    }
}

#[derive(Clone, Debug)]
pub(crate) struct FilePatch {
    pub path: String,
    pub is_new: bool,
    pub is_delete: bool,
    pub hunks: Vec<Hunk>,
}

#[derive(Clone, Debug)]
pub(crate) struct Hunk {
    old_start: usize,
    lines: Vec<HunkLine>,
}

#[derive(Clone, Debug)]
enum HunkLine {
    Ctx(String),
    Del(String),
    Add(String),
}

pub struct AtomicDiffPatcher {
    root: PathBuf,
}

impl AtomicDiffPatcher {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    pub fn apply(&self, diff: &str) -> Result<PatchReport, String> {
        Ok(crate::tools::atomic_fs::AtomicBatchPatcher::new(&self.root).apply(diff))
    }

    /// Apply `unified_diff` for `target_file` in a RAM buffer. Never writes disk.
    pub fn apply_diff_in_memory(
        &self,
        target_file: &str,
        unified_diff: &str,
    ) -> Result<String, PatchError> {
        apply_diff_in_memory(&self.root, target_file, unified_diff)
    }
}

/// Parse and apply a unified diff against one file in virtual memory, then Tree-Sitter-gate it.
pub fn apply_diff_in_memory(
    root: &std::path::Path,
    target_file: &str,
    unified_diff: &str,
) -> Result<String, PatchError> {
    let text = strip_fence(unified_diff);
    let files = parse_unified_diff(&text).map_err(PatchError::Parse)?;
    if files.is_empty() {
        return Err(PatchError::Parse("diff contained no files".into()));
    }
    let rel = target_file.replace('\\', "/");
    let file = files
        .iter()
        .find(|f| f.path == rel || f.path.ends_with(&rel) || rel.ends_with(&f.path))
        .or_else(|| {
            if files.len() == 1 {
                files.first()
            } else {
                None
            }
        })
        .ok_or_else(|| PatchError::Parse(format!("diff has no hunks for {rel}")))?;
    if file.is_delete {
        return Ok(String::new());
    }
    let dest = confined_join(root, &file.path).map_err(PatchError::Io)?;
    let source = read_source(&dest).map_err(PatchError::Io)?;
    let updated = apply_hunks(&source, &file.hunks).map_err(PatchError::Hunk)?;
    if let Some(lang) = Lang::from_path(&dest) {
        let mut parser = AstParser::new();
        if let Some(hit) = parser.syntax_errors(lang, updated.as_bytes()).into_iter().next() {
            return Err(PatchError::ast(&file.path, &hit));
        }
    }
    Ok(updated)
}

pub(crate) fn read_source(path: &std::path::Path) -> Result<String, String> {
    if !path.is_file() {
        return Ok(String::new());
    }
    if let Some(mapped) = crate::mmap_search::map_or_read(path) {
        return Ok(String::from_utf8_lossy(&mapped).into_owned());
    }
    std::fs::read_to_string(path).map_err(|e| e.to_string())
}

pub(crate) fn strip_fence(diff: &str) -> String {
    let trimmed = diff.trim();
    if let Some(rest) = trimmed.strip_prefix("```diff") {
        if let Some(end) = rest.rfind("```") {
            return rest[..end].trim().to_string();
        }
    }
    if let Some(rest) = trimmed.strip_prefix("```") {
        if let Some(end) = rest.rfind("```") {
            return rest[..end].trim().to_string();
        }
    }
    trimmed.to_string()
}

pub(crate) fn parse_unified_diff(diff: &str) -> Result<Vec<FilePatch>, String> {
    let mut files: Vec<FilePatch> = Vec::new();
    let mut current: Option<FilePatch> = None;
    let mut pending_old: Option<String> = None;

    for raw in diff.lines() {
        let line = raw.trim_end_matches('\r');
        if let Some(rest) = line.strip_prefix("diff --git ") {
            if let Some(file) = current.take() {
                files.push(file);
            }
            pending_old = None;
            let path = rest
                .split_whitespace()
                .nth(1)
                .map(strip_ab)
                .unwrap_or_default();
            if !path.is_empty() && path != "/dev/null" {
                current = Some(FilePatch {
                    path,
                    is_new: false,
                    is_delete: false,
                    hunks: Vec::new(),
                });
            }
            continue;
        }
        if let Some(rest) = line.strip_prefix("--- ") {
            pending_old = Some(strip_ab(rest.trim()));
            continue;
        }
        if let Some(rest) = line.strip_prefix("+++ ") {
            let new_path = strip_ab(rest.trim());
            let old_path = pending_old.take().unwrap_or_default();
            if let Some(file) = current.take() {
                files.push(file);
            }
            if new_path == "/dev/null" {
                current = Some(FilePatch {
                    path: if old_path == "/dev/null" {
                        String::new()
                    } else {
                        old_path
                    },
                    is_new: false,
                    is_delete: true,
                    hunks: Vec::new(),
                });
            } else {
                current = Some(FilePatch {
                    path: new_path,
                    is_new: old_path == "/dev/null",
                    is_delete: false,
                    hunks: Vec::new(),
                });
            }
            continue;
        }
        if let Some(rest) = line.strip_prefix("@@ ") {
            let hunk = parse_hunk_header(rest)?;
            if let Some(file) = current.as_mut() {
                file.hunks.push(hunk);
            } else {
                return Err("hunk without file header".into());
            }
            continue;
        }
        if let Some(file) = current.as_mut() {
            if let Some(hunk) = file.hunks.last_mut() {
                if line.starts_with('\\') {
                    continue;
                }
                if let Some(text) = line.strip_prefix('+') {
                    if !line.starts_with("+++") {
                        hunk.lines.push(HunkLine::Add(text.to_string()));
                    }
                } else if let Some(text) = line.strip_prefix('-') {
                    if !line.starts_with("---") {
                        hunk.lines.push(HunkLine::Del(text.to_string()));
                    }
                } else if let Some(text) = line.strip_prefix(' ') {
                    hunk.lines.push(HunkLine::Ctx(text.to_string()));
                }
            }
        }
    }
    if let Some(file) = current.take() {
        files.push(file);
    }
    files.retain(|f| !f.path.is_empty());
    Ok(files)
}

fn strip_ab(path: &str) -> String {
    let p = path.trim();
    let p = p.strip_prefix("a/").or_else(|| p.strip_prefix("b/")).unwrap_or(p);
    p.split('\t').next().unwrap_or(p).trim().to_string()
}

fn parse_hunk_header(rest: &str) -> Result<Hunk, String> {
    let old = rest
        .split_whitespace()
        .next()
        .ok_or_else(|| "bad hunk header".to_string())?;
    let old = old.trim_start_matches('-');
    let start = old
        .split(',')
        .next()
        .and_then(|s| s.parse::<usize>().ok())
        .ok_or_else(|| "bad hunk old start".to_string())?;
    let old_start = if start == 0 { 0 } else { start - 1 };
    Ok(Hunk {
        old_start,
        lines: Vec::new(),
    })
}

pub(crate) fn apply_hunks(source: &str, hunks: &[Hunk]) -> Result<String, String> {
    if hunks.is_empty() {
        return Err("diff contained no hunks".into());
    }
    let (mut lines, trailing) = split_lines(source);
    let mut cursor_adjust: isize = 0;
    for hunk in hunks {
        let old_img = old_image(hunk);
        let new_img = new_image(hunk);
        let hint = (hunk.old_start as isize + cursor_adjust).max(0) as usize;
        let at = find_anchor(&lines, &old_img, hint)?;
        let end = at + old_img.len();
        if end > lines.len() {
            return Err("hunk is outside the file".into());
        }
        lines.splice(at..end, new_img.iter().cloned());
        cursor_adjust += new_img.len() as isize - old_img.len() as isize;
    }
    Ok(join_lines(&lines, trailing || source.ends_with('\n')))
}

fn old_image(hunk: &Hunk) -> Vec<String> {
    hunk.lines
        .iter()
        .filter_map(|l| match l {
            HunkLine::Ctx(s) | HunkLine::Del(s) => Some(s.clone()),
            HunkLine::Add(_) => None,
        })
        .collect()
}

fn new_image(hunk: &Hunk) -> Vec<String> {
    hunk.lines
        .iter()
        .filter_map(|l| match l {
            HunkLine::Ctx(s) | HunkLine::Add(s) => Some(s.clone()),
            HunkLine::Del(_) => None,
        })
        .collect()
}

fn find_anchor(lines: &[String], needle: &[String], hint: usize) -> Result<usize, String> {
    if needle.is_empty() {
        return Ok(hint.min(lines.len()));
    }
    if matches_at(lines, hint, needle, MatchMode::Exact) {
        return Ok(hint);
    }
    let last = lines.len().saturating_sub(needle.len());
    for i in 0..=last {
        if matches_at(lines, i, needle, MatchMode::Exact) {
            return Ok(i);
        }
    }
    for i in 0..=last {
        if matches_at(lines, i, needle, MatchMode::Whitespace) {
            return Ok(i);
        }
    }
    for i in 0..=last {
        if matches_at(lines, i, needle, MatchMode::Fuzzy) {
            return Ok(i);
        }
    }
    Err("hunk does not match file (fuzzy search failed)".into())
}

#[derive(Clone, Copy)]
enum MatchMode {
    Exact,
    Whitespace,
    Fuzzy,
}

fn matches_at(lines: &[String], at: usize, needle: &[String], mode: MatchMode) -> bool {
    if at + needle.len() > lines.len() {
        return false;
    }
    needle.iter().enumerate().all(|(i, want)| {
        let got = &lines[at + i];
        match mode {
            MatchMode::Exact => got == want,
            MatchMode::Whitespace => got.trim() == want.trim(),
            MatchMode::Fuzzy => levenshtein(got.trim(), want.trim()) <= 2,
        }
    })
}

fn split_lines(s: &str) -> (Vec<String>, bool) {
    let trailing = s.ends_with('\n') || s.ends_with('\r');
    let normalized = s.replace("\r\n", "\n").replace('\r', "\n");
    let mut lines: Vec<String> = normalized.split('\n').map(|p| p.to_string()).collect();
    if trailing && lines.last().map(|l| l.is_empty()).unwrap_or(false) {
        lines.pop();
    }
    (lines, trailing)
}

fn join_lines(lines: &[String], trailing: bool) -> String {
    let mut s = lines.join("\n");
    if trailing && !s.ends_with('\n') {
        s.push('\n');
    }
    s
}

fn levenshtein(a: &str, b: &str) -> usize {
    if a == b {
        return 0;
    }
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    let (n, m) = (a.len(), b.len());
    if n.abs_diff(m) > 4 || n > 120 || m > 120 {
        return 99;
    }
    let mut prev: Vec<usize> = (0..=m).collect();
    let mut cur = vec![0usize; m + 1];
    for i in 1..=n {
        cur[0] = i;
        for j in 1..=m {
            let cost = usize::from(a[i - 1] != b[j - 1]);
            cur[j] = (prev[j] + 1)
                .min(cur[j - 1] + 1)
                .min(prev[j - 1] + cost);
        }
        std::mem::swap(&mut prev, &mut cur);
    }
    prev[m]
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn scratch() -> PathBuf {
        let n = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("robber-diff-{n}"));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn applies_multi_file_and_rolls_back_syntax() {
        let dir = scratch();
        fs::write(dir.join("a.py"), "def a():\n    return 1\n").unwrap();
        fs::write(dir.join("b.py"), "def b():\n    return 2\n").unwrap();
        let patcher = AtomicDiffPatcher::new(&dir);
        let ok = patcher
            .apply(
                "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n def a():\n-    return 1\n+    return 10\n--- a/b.py\n+++ b/b.py\n@@ -1,2 +1,2 @@\n def b():\n-    return 2\n+    return 20\n",
            )
            .unwrap();
        assert!(ok.ok, "{}", ok.error);
        assert_eq!(ok.files.len(), 2);
        assert!(fs::read_to_string(dir.join("a.py")).unwrap().contains("return 10"));
        assert!(fs::read_to_string(dir.join("b.py")).unwrap().contains("return 20"));

        let bad = patcher
            .apply("--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,1 @@\n-def a():\n-    return 10\n+def a(\n")
            .unwrap();
        assert!(!bad.ok);
        assert!(bad.rolled_back);
        assert_eq!(bad.error_kind, "ast");
        assert!(bad.error.contains("AstValidationError"));
        assert!(bad.elapsed_us < 20_000 || bad.elapsed_ms < 20);
        assert!(fs::read_to_string(dir.join("a.py")).unwrap().contains("return 10"));
        assert!(fs::read_to_string(dir.join("b.py")).unwrap().contains("return 20"));
        let mem = patcher.apply_diff_in_memory("a.py", "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,1 @@\n-def a():\n-    return 10\n+def a(\n");
        assert!(matches!(mem, Err(PatchError::AstValidation(_))));
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn in_memory_apply_does_not_touch_disk() {
        let dir = scratch();
        let original = "def a():\n    return 1\n";
        fs::write(dir.join("mod.py"), original).unwrap();
        let patcher = AtomicDiffPatcher::new(&dir);
        let ok = patcher
            .apply_diff_in_memory(
                "mod.py",
                "--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n def a():\n-    return 1\n+    return 9\n",
            )
            .unwrap();
        assert!(ok.contains("return 9"));
        assert_eq!(fs::read_to_string(dir.join("mod.py")).unwrap(), original);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn fuzzy_matches_whitespace_drift() {
        let dir = scratch();
        fs::write(dir.join("a.py"), "def a():\n    return  1\n").unwrap();
        let patcher = AtomicDiffPatcher::new(&dir);
        let report = patcher
            .apply("--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n def a():\n-    return 1\n+    return 3\n")
            .unwrap();
        assert!(report.ok, "{}", report.error);
        assert!(fs::read_to_string(dir.join("a.py")).unwrap().contains("return 3"));
        fs::remove_dir_all(&dir).ok();
    }
}
