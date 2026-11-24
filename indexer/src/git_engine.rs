//! AST-aware 3-way merge, conventional-commit classification, and branch naming.
//!
//! Git porcelain (checkout, stash, commit, push) stays on the Python side
//! (`robber.tools.git_manager`) using the local `git` binary. This crate does
//! not pull git2/libgit2 — libgit2 would blow the 200 MB indexer budget on
//! 8GB RAM / i5 hardware. Enable the optional `git2` Cargo feature only in
//! dedicated native builds.

use std::collections::{BTreeMap, BTreeSet};

use crate::ast_parser::{AstParser, FileAst, Lang, SymbolHit};

#[derive(Debug, Clone)]
pub struct ConflictError {
    pub symbol: String,
    pub detail: String,
}

impl std::fmt::Display for ConflictError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "conflict on {}: {}", self.symbol, self.detail)
    }
}

/// Semantic merge: union imports and non-overlapping function/class additions.
pub fn ast_merge(base: &str, ours: &str, theirs: &str) -> Result<String, ConflictError> {
    let lang = sniff(ours).or_else(|| sniff(theirs)).or_else(|| sniff(base)).unwrap_or(Lang::Python);
    ast_merge_lang(lang, base, ours, theirs)
}

pub fn ast_merge_lang(lang: Lang, base: &str, ours: &str, theirs: &str) -> Result<String, ConflictError> {
    let mut parser = AstParser::new();
    let base_ast = parser
        .parse_source(lang, base.as_bytes(), "base")
        .ok_or_else(|| ConflictError {
            symbol: "module".into(),
            detail: "base failed to parse".into(),
        })?;
    let ours_ast = parser
        .parse_source(lang, ours.as_bytes(), "ours")
        .ok_or_else(|| ConflictError {
            symbol: "module".into(),
            detail: "ours failed to parse".into(),
        })?;
    let theirs_ast = parser
        .parse_source(lang, theirs.as_bytes(), "theirs")
        .ok_or_else(|| ConflictError {
            symbol: "module".into(),
            detail: "theirs failed to parse".into(),
        })?;

    let base_mod = view(base, &base_ast);
    let ours_mod = view(ours, &ours_ast);
    let theirs_mod = view(theirs, &theirs_ast);

    let mut imports: BTreeSet<String> = BTreeSet::new();
    for row in base_mod.imports.iter().chain(ours_mod.imports.iter()).chain(theirs_mod.imports.iter()) {
        if !row.is_empty() {
            imports.insert(row.clone());
        }
    }

    let mut order: Vec<String> = Vec::new();
    let mut seen = BTreeSet::new();
    for name in ours_mod.order.iter().chain(theirs_mod.order.iter()) {
        if seen.insert(name.clone()) {
            order.push(name.clone());
        }
    }

    let mut blocks: BTreeMap<String, String> = BTreeMap::new();
    for name in &order {
        let ours_b = ours_mod.blocks.get(name);
        let theirs_b = theirs_mod.blocks.get(name);
        let base_b = base_mod.blocks.get(name);
        match (ours_b, theirs_b) {
            (None, None) => {}
            (None, Some(text)) => {
                blocks.insert(name.clone(), text.clone());
            }
            (Some(text), None) => {
                blocks.insert(name.clone(), text.clone());
            }
            (Some(left), Some(right)) if left == right => {
                blocks.insert(name.clone(), left.clone());
            }
            (Some(left), Some(right)) => {
                if base_b.map(|b| b == left).unwrap_or(false) {
                    blocks.insert(name.clone(), right.clone());
                } else if base_b.map(|b| b == right).unwrap_or(false) {
                    blocks.insert(name.clone(), left.clone());
                } else {
                    return Err(ConflictError {
                        symbol: name.clone(),
                        detail: "both sides changed this symbol".into(),
                    });
                }
            }
        }
    }

    let mut out = String::new();
    let has_imports = !imports.is_empty();
    for line in imports {
        out.push_str(&line);
        out.push('\n');
    }
    if has_imports {
        out.push('\n');
    }
    for name in order {
        if let Some(body) = blocks.get(&name) {
            out.push_str(body.trim_end());
            out.push_str("\n\n");
        }
    }
    if !out.ends_with('\n') {
        out.push('\n');
    }
    Ok(out)
}

pub fn commit_message(base: &str, ours: &str) -> String {
    let lang = sniff(ours).or_else(|| sniff(base)).unwrap_or(Lang::Python);
    let mut parser = AstParser::new();
    let Some(base_ast) = parser.parse_source(lang, base.as_bytes(), "base") else {
        return "refactor: sync sources".into();
    };
    let Some(ours_ast) = parser.parse_source(lang, ours.as_bytes(), "ours") else {
        return "refactor: sync sources".into();
    };
    let base_names: BTreeSet<String> = base_ast.all_symbols().map(|s| s.name.clone()).collect();
    let ours_view = view(ours, &ours_ast);
    let mut added = Vec::new();
    let mut changed = Vec::new();
    for name in &ours_view.order {
        if !base_names.contains(name) {
            added.push(name.clone());
        } else if ours_view.blocks.get(name) != view(base, &base_ast).blocks.get(name) {
            changed.push(name.clone());
        }
    }
    if !added.is_empty() && added.iter().all(|n| n.starts_with("test_")) && changed.is_empty() {
        return format!("test: add {}", added.into_iter().take(3).collect::<Vec<_>>().join(", "));
    }
    if !added.is_empty() && changed.is_empty() {
        return format!("feat: add {}", added.into_iter().take(3).collect::<Vec<_>>().join(", "));
    }
    if !changed.is_empty() && added.is_empty() {
        return format!("refactor: update {}", changed.into_iter().take(3).collect::<Vec<_>>().join(", "));
    }
    if !changed.is_empty() || !added.is_empty() {
        let mixed: Vec<String> = changed.into_iter().chain(added).take(3).collect();
        return format!("refactor: update {}", mixed.join(", "));
    }
    "refactor: sync sources".into()
}

/// Isolated agent branch: `agent/feat-[task_id]-[slug]`.
/// UUID-like task ids are omitted so the slug remains human-readable.
pub fn feature_branch_name(task_id: &str, slug: &str) -> String {
    let id = slugify(task_id);
    let slug = slugify(slug);
    let slug = if slug.is_empty() { "task" } else { slug.as_str() };
    let slug = if slug.len() > 40 { &slug[..40] } else { slug };
    if id.is_empty() || id == slug || looks_uuid(&id) {
        return format!("agent/feat-{slug}");
    }
    let id = if id.len() > 12 { &id[..12] } else { &id };
    format!("agent/feat-{id}-{slug}")
}

fn looks_uuid(raw: &str) -> bool {
    raw.len() >= 8 && raw.chars().all(|c| c.is_ascii_hexdigit())
}

pub fn slugify(raw: &str) -> String {
    let mut out = String::new();
    let mut dash = false;
    for ch in raw.chars() {
        if ch.is_ascii_alphanumeric() {
            out.push(ch.to_ascii_lowercase());
            dash = false;
        } else if !out.is_empty() && !dash {
            out.push('-');
            dash = true;
        }
    }
    out.trim_matches('-').to_string()
}

/// Classify a mutation from changed paths + AST symbol delta.
pub fn classify_kind(paths: &[&str], base: &str, ours: &str) -> &'static str {
    if !paths.is_empty() && paths.iter().all(|p| is_test_path(p)) {
        return "test";
    }
    let msg = commit_message(base, ours);
    if msg.starts_with("test:") {
        return "test";
    }
    if msg.starts_with("feat:") {
        return "feat";
    }
    if msg.starts_with("fix:") || looks_like_fix(base, ours, paths) {
        return "fix";
    }
    if msg.starts_with("refactor:") {
        return "refactor";
    }
    "chore"
}

pub fn conventional_commit(base: &str, ours: &str, paths: &[&str], scope: &str) -> String {
    let kind = classify_kind(paths, base, ours);
    let subject = commit_message(base, ours);
    let rest = subject.splitn(2, ':').nth(1).unwrap_or(" sync sources").trim();
    let scope = slugify(scope);
    if scope.is_empty() {
        format!("{kind}: {rest}")
    } else {
        format!("{kind}({scope}): {rest}")
    }
}

pub fn commit_footer(paths: &[&str], base: &str, ours: &str) -> String {
    let lang = sniff(ours).or_else(|| sniff(base)).unwrap_or(Lang::Python);
    let mut parser = AstParser::new();
    let base_ast = parser.parse_source(lang, base.as_bytes(), "base");
    let ours_ast = parser.parse_source(lang, ours.as_bytes(), "ours");
    let base_names: BTreeSet<String> = base_ast
        .as_ref()
        .map(|a| a.all_symbols().map(|s| s.name.clone()).collect())
        .unwrap_or_default();
    let ours_names: BTreeSet<String> = ours_ast
        .as_ref()
        .map(|a| a.all_symbols().map(|s| s.name.clone()).collect())
        .unwrap_or_default();
    let added: Vec<_> = ours_names.difference(&base_names).cloned().take(8).collect();
    let removed: Vec<_> = base_names.difference(&ours_names).cloned().take(8).collect();
    let mut lines = Vec::new();
    if !paths.is_empty() {
        lines.push(format!("Files: {}", paths.iter().take(8).cloned().collect::<Vec<_>>().join(", ")));
    }
    if !added.is_empty() {
        lines.push(format!("Added symbols: {}", added.join(", ")));
    }
    if !removed.is_empty() {
        lines.push(format!("Removed symbols: {}", removed.join(", ")));
    }
    lines.join("\n")
}

fn is_test_path(path: &str) -> bool {
    let lower = path.replace('\\', "/").to_ascii_lowercase();
    let name = lower.rsplit('/').next().unwrap_or(&lower);
    lower.contains("/test/")
        || lower.contains("/tests/")
        || lower.contains("__tests__")
        || name.starts_with("test_")
        || name.ends_with("_test.py")
        || name.ends_with(".test.ts")
        || name.ends_with(".test.js")
        || name.ends_with("_test.go")
        || name.ends_with("_spec.rb")
}

fn looks_like_fix(base: &str, ours: &str, paths: &[&str]) -> bool {
    let blob = format!("{base}\n{ours}\n{}", paths.join(" "));
    let lower = blob.to_ascii_lowercase();
    lower.contains("fix") || lower.contains("bug") || lower.contains("assert")
}

fn sniff(src: &str) -> Option<Lang> {
    if src.contains("def ") || src.contains("class ") {
        Some(Lang::Python)
    } else if src.contains("package ") && src.contains("func ") {
        Some(Lang::Go)
        } else if src.contains("pub fn ") || (src.contains("fn ") && src.contains("struct ")) {
        Some(Lang::Rust)
    } else if src.contains("export function") || src.contains("export class") {
        Some(Lang::TypeScript)
    } else {
        None
    }
}

struct ModuleView {
    imports: Vec<String>,
    blocks: BTreeMap<String, String>,
    order: Vec<String>,
}

fn view(src: &str, file: &FileAst) -> ModuleView {
    let lines: Vec<&str> = src.split('\n').collect();
    let first = file
        .all_symbols()
        .map(|s| s.line)
        .min()
        .unwrap_or((lines.len() as u32) + 1);
    let mut imports = Vec::new();
    for (idx, line) in lines.iter().enumerate() {
        let n = (idx as u32) + 1;
        if n >= first {
            break;
        }
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') || trimmed.starts_with("//") {
            continue;
        }
        if is_import_line(trimmed) && !imports.iter().any(|e: &String| e == trimmed) {
            imports.push(trimmed.to_string());
        }
    }
    let mut blocks = BTreeMap::new();
    let mut order = Vec::new();
    let mut seen = BTreeSet::new();
    for hit in file.all_symbols() {
        if !seen.insert(hit.name.clone()) {
            continue;
        }
        order.push(hit.name.clone());
        blocks.insert(hit.name.clone(), slice_symbol(src, hit));
    }
    ModuleView {
        imports,
        blocks,
        order,
    }
}

fn is_import_line(line: &str) -> bool {
    line.starts_with("import ")
        || line.starts_with("from ")
        || line.starts_with("use ")
        || line.starts_with("package ")
}

fn slice_symbol(src: &str, hit: &SymbolHit) -> String {
    let lines: Vec<&str> = src.split('\n').collect();
    let start = hit.line.saturating_sub(1) as usize;
    let end = (hit.end_line as usize).max(start + 1).min(lines.len());
    lines[start.min(lines.len())..end].join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn merges_nonoverlapping_functions_and_imports() {
        let base = "def foo():\n    return 1\n";
        let ours = "import os\n\ndef foo():\n    return 1\n\ndef bar():\n    return 2\n";
        let theirs = "import sys\n\ndef foo():\n    return 1\n\ndef baz():\n    return 3\n";
        let merged = ast_merge(base, ours, theirs).unwrap();
        assert!(merged.contains("import os"));
        assert!(merged.contains("import sys"));
        assert!(merged.contains("def bar()"));
        assert!(merged.contains("def baz()"));
        assert!(!merged.contains("<<<<<<<"));
    }

    #[test]
    fn commit_message_feat_on_new_symbol() {
        let base = "def foo():\n    return 1\n";
        let ours = "def foo():\n    return 1\n\ndef bar():\n    return 2\n";
        let msg = commit_message(base, ours);
        assert!(msg.starts_with("feat:"));
    }

    #[test]
    fn feature_branch_uses_task_and_slug() {
        let name = feature_branch_name("json-logger-helper", "Json Log Parser");
        assert!(name.starts_with("agent/feat-"));
        assert!(name.contains("json-logger"));
    }

    #[test]
    fn test_paths_classify_as_test() {
        let kind = classify_kind(&["tests/test_parser.py"], "def foo():\n    return 1\n", "def foo():\n    return 1\n");
        assert_eq!(kind, "test");
    }

    #[test]
    fn scoped_feat_commit() {
        let base = "def foo():\n    return 1\n";
        let ours = "def foo():\n    return 1\n\nclass JsonLogParser:\n    pass\n";
        let msg = conventional_commit(base, ours, &["logger.py"], "logger");
        assert!(msg.starts_with("feat(logger):"));
    }
}
