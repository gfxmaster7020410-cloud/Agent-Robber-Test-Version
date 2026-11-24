//! Agent Robber native indexer: AST parse, symbol graph, mmap search, SSOT sync.

pub mod ast_parser;
pub mod crawl;
pub mod ffi;
pub mod git_engine;
pub mod hashutil;
pub mod mmap_search;
pub mod multi_repo;
pub mod py_bindings;
pub mod search;
pub mod security_audit;
pub mod ssot_sync;
pub mod symbol_graph;
pub mod symbol_search;
pub mod tools;

use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

use rayon::prelude::*;
use serde::Serialize;

use ast_parser::{AstParser, FileAst, Lang, MAX_PARSE_BYTES};
use crawl::{collect_sources, relative_posix};
use hashutil::sha3_hex;
use mmap_search::MmapSearcher;
use ssot_sync::SsotSync;
use symbol_graph::SymbolGraph;

#[derive(Clone, Debug, Default, Serialize)]
pub struct IndexReport {
    pub files_seen: usize,
    pub files_parsed: usize,
    pub files_skipped: usize,
    pub symbols: usize,
    pub rows_upserted: usize,
    pub edges: usize,
    pub elapsed_ms: u128,
    pub project_id: String,
}

pub struct Indexer {
    pub root: PathBuf,
    pub max_file_bytes: u64,
}

impl Indexer {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self {
            root: root.into(),
            max_file_bytes: MAX_PARSE_BYTES,
        }
    }

    pub fn index_into(&self, db_path: &Path) -> Result<IndexReport, String> {
        let started = Instant::now();
        let root = fs::canonicalize(&self.root).unwrap_or_else(|_| self.root.clone());
        let files = collect_sources(&root);
        let mut sync = SsotSync::open(db_path)?;
        let name = root
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("workspace");
        let project_id = sync.ensure_project(&root, name)?;
        let known = sync.existing_hashes(&project_id).unwrap_or_default();
        let max_bytes = self.max_file_bytes;

        let parsed: Vec<FileAst> = files
            .par_iter()
            .filter_map(|path| parse_if_changed(path, &root, &known, max_bytes))
            .collect();

        let skipped = files.len().saturating_sub(parsed.len());
        let mut graph = SymbolGraph::default();
        let mut symbols = 0usize;
        for file in &parsed {
            symbols += file.all_symbols().count();
            graph.ingest(file);
        }
        graph.link_files();

        let rows = sync.upsert_files(&project_id, &parsed)?;
        let telemetry = serde_json::json!({
            "files": files.len(),
            "parsed": parsed.len(),
            "symbols": symbols,
            "graph": graph.fingerprint(),
        })
        .to_string();
        let _ = sync.touch_agent(&project_id, &telemetry);

        Ok(IndexReport {
            files_seen: files.len(),
            files_parsed: parsed.len(),
            files_skipped: skipped,
            symbols,
            rows_upserted: rows,
            edges: graph.edges.len(),
            elapsed_ms: started.elapsed().as_millis(),
            project_id,
        })
    }

    pub fn search(&self, pattern: &str, max_hits: usize) -> Result<Vec<mmap_search::SearchHit>, String> {
        let mut searcher = MmapSearcher { max_hits };
        if searcher.max_hits == 0 {
            searcher.max_hits = 256;
        }
        searcher.search_tree(&self.root, pattern)
    }

    /// Compact JSON AST neighborhood. PyO3-shaped: `(query_symbol, depth) -> String`.
    pub fn get_symbol_context(&self, query_symbol: &str, depth: usize) -> String {
        symbol_graph::get_symbol_context(&self.root, query_symbol, depth)
    }
}

fn parse_if_changed(
    path: &Path,
    root: &Path,
    known: &std::collections::HashMap<String, String>,
    max_bytes: u64,
) -> Option<FileAst> {
    let lang = Lang::from_path(path)?;
    let meta = fs::metadata(path).ok()?;
    if meta.len() == 0 || meta.len() > max_bytes {
        return None;
    }
    let bytes = fs::read(path).ok()?;
    let digest = sha3_hex(&bytes);
    let rel = relative_posix(root, path);
    if known.get(&rel).map(|h| h == &digest).unwrap_or(false) {
        return None;
    }
    let mut parser = AstParser::new();
    parser.parse_source(lang, &bytes, &rel)
}

#[cfg(windows)]
pub fn rss_bytes() -> u64 {
    winmem::current_working_set()
}

#[cfg(windows)]
pub(crate) mod winmem {
    use std::mem::{size_of, zeroed};

    #[repr(C)]
    pub struct ProcessMemoryCountersEx {
        pub cb: u32,
        pub page_fault_count: u32,
        pub peak_working_set_size: usize,
        pub working_set_size: usize,
        pub quota_peak_paged_pool_usage: usize,
        pub quota_paged_pool_usage: usize,
        pub quota_peak_non_paged_pool_usage: usize,
        pub quota_non_paged_pool_usage: usize,
        pub pagefile_usage: usize,
        pub peak_pagefile_usage: usize,
        pub private_usage: usize,
    }

    #[link(name = "psapi")]
    extern "system" {
        fn GetCurrentProcess() -> *mut core::ffi::c_void;
        fn GetProcessMemoryInfo(
            handle: *mut core::ffi::c_void,
            counters: *mut ProcessMemoryCountersEx,
            size: u32,
        ) -> i32;
    }

    pub fn current_working_set() -> u64 {
        query(unsafe { GetCurrentProcess() }).0
    }

    pub fn private_bytes(handle: *mut core::ffi::c_void) -> u64 {
        query(handle).1
    }

    fn query(handle: *mut core::ffi::c_void) -> (u64, u64) {
        if handle.is_null() {
            return (0, 0);
        }
        unsafe {
            let mut info: ProcessMemoryCountersEx = zeroed();
            info.cb = size_of::<ProcessMemoryCountersEx>() as u32;
            if GetProcessMemoryInfo(handle, &mut info, info.cb) != 0 {
                let working = info.working_set_size as u64;
                let private = info.private_usage as u64;
                return (working, if private > 0 { private } else { working });
            }
        }
        (0, 0)
    }
}

#[cfg(not(windows))]
pub fn rss_bytes() -> u64 {
    0
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn scratch(tag: &str) -> PathBuf {
        let n = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("robber-idx-{tag}-{n}"));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn indexes_four_languages_into_ssot() {
        let dir = scratch("langs");
        fs::write(dir.join("a.py"), "import b\nclass A:\n    def run(self):\n        return helper()\ndef helper():\n    return 1\n").unwrap();
        fs::write(dir.join("b.ts"), "export class Box {}\nexport function make(): Box { return new Box(); }\n").unwrap();
        fs::write(dir.join("c.go"), "package p\nfunc Sum(a int) int { return a }\n").unwrap();
        fs::write(dir.join("d.rs"), "pub struct S;\npub fn ready() -> S { S }\n").unwrap();
        let db = dir.join("ssot.db");
        let report = Indexer::new(&dir).index_into(&db).unwrap();
        assert!(report.files_parsed >= 4);
        assert!(report.symbols >= 4);
        assert!(report.rows_upserted >= 4);
        let sync = SsotSync::open(&db).unwrap();
        assert!(sync.count_codebase(&report.project_id).unwrap() >= 4);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn incremental_skips_unchanged_hash() {
        let dir = scratch("inc");
        fs::write(dir.join("x.py"), "def once():\n    return 1\n").unwrap();
        let db = dir.join("ssot.db");
        let first = Indexer::new(&dir).index_into(&db).unwrap();
        let second = Indexer::new(&dir).index_into(&db).unwrap();
        assert!(first.files_parsed >= 1);
        assert_eq!(second.files_parsed, 0);
        fs::write(dir.join("x.py"), "def once():\n    return 2\n").unwrap();
        let third = Indexer::new(&dir).index_into(&db).unwrap();
        assert_eq!(third.files_parsed, 1);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn ten_k_files_under_memory_budget() {
        let dir = scratch("10k");
        (0..10_000).into_par_iter().for_each(|i| {
            let body = format!("def f{i}():\n    return {i}\n");
            let _ = fs::write(dir.join(format!("f{i}.py")), body);
        });
        let before = rss_bytes();
        let db = dir.join("ssot.db");
        let report = Indexer::new(&dir).index_into(&db).unwrap();
        let after = rss_bytes();
        assert_eq!(report.files_seen, 10_000);
        assert!(report.files_parsed >= 9_900);
        assert!(report.rows_upserted >= 9_900);
        if after > 0 {
            let used_mb = after as f64 / (1024.0 * 1024.0);
            assert!(
                used_mb < 100.0,
                "rss {used_mb:.1} MB (before {:.1})",
                before as f64 / (1024.0 * 1024.0)
            );
        }
        fs::remove_dir_all(&dir).ok();
    }
}
