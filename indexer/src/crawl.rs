//! Repository walk that skips jail/secret/build trees and binary files.

use std::path::{Path, PathBuf};

use walkdir::{DirEntry, WalkDir};

const SKIP_DIRS: &[&str] = &[
    "node_modules",
    "dist",
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    ".robber",
    ".cursor",
    "target",
    ".pytest_cache",
    ".mypy_cache",
];

pub(crate) const SECRET_NAMES: &[&str] = &[".env", ".env.local", "keys.vault", "credentials.json"];

const SOURCE_EXTS: &[&str] = &["py", "ts", "tsx", "js", "jsx", "mjs", "cjs", "go", "rs"];

pub fn is_skipped_dir(name: &str) -> bool {
    SKIP_DIRS.contains(&name) || name.starts_with('.')
}

pub fn is_source_file(path: &Path) -> bool {
    let name = path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("");
    if SECRET_NAMES.contains(&name) {
        return false;
    }
    path.extension()
        .and_then(|e| e.to_str())
        .map(|ext| {
            let lower = ext.to_ascii_lowercase();
            SOURCE_EXTS.contains(&lower.as_str())
        })
        .unwrap_or(false)
}

fn skip_entry(entry: &DirEntry) -> bool {
    if entry.file_type().is_dir() {
        if let Some(name) = entry.file_name().to_str() {
            return is_skipped_dir(name);
        }
    }
    false
}

/// Collect source files under `root`. Paths are absolute.
pub fn collect_sources(root: &Path) -> Vec<PathBuf> {
    WalkDir::new(root)
        .follow_links(false)
        .into_iter()
        .filter_entry(|e| !skip_entry(e))
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().is_file() && is_source_file(e.path()))
        .map(|e| e.into_path())
        .collect()
}

pub fn relative_posix(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
}
