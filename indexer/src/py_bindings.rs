//! In-process C ABI for Python ctypes (zero IPC).
//!
//! PyO3 is intentionally not a default dependency: the GNU Windows build
//! must stay green without Python headers. Load this cdylib from Python
//! (`robber.indexer_bridge`) the same way as `ffi.rs`. A `python` Cargo
//! feature can wrap these symbols in `#[pyfunction]` later.

use std::os::raw::{c_char, c_int};
use std::path::Path;
use std::time::Duration;

use crate::ffi::{c_str, set_err, write_out};
use crate::tools::{
    apply_diff_atomic, quick_search, run_test_suite, run_tool_sandboxed, DEFAULT_TEST_TIMEOUT,
    DEFAULT_TIMEOUT,
};

fn opt_path(raw: *const c_char) -> Result<Option<String>, String> {
    if raw.is_null() {
        return Ok(None);
    }
    let s = c_str(raw)?;
    if s.is_empty() {
        Ok(None)
    } else {
        Ok(Some(s.to_string()))
    }
}

/// Run `program` + JSON argv under the 250MB sandbox. Optional SSOT log path.
#[no_mangle]
pub extern "C" fn robber_run_tool_sandboxed(
    root: *const c_char,
    program: *const c_char,
    args_json: *const c_char,
    cwd: *const c_char,
    timeout_ms: u32,
    ssot_db: *const c_char,
    out: *mut c_char,
    out_len: u32,
) -> c_int {
    let root = match c_str(root) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let program = match c_str(program) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let args: Vec<String> = match opt_path(args_json) {
        Ok(Some(raw)) => match serde_json::from_str::<Vec<String>>(&raw) {
            Ok(v) => v,
            Err(e) => {
                set_err(&format!("args_json: {e}"));
                return -1;
            }
        },
        Ok(None) => Vec::new(),
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let cwd = match opt_path(cwd) {
        Ok(v) => v,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let db = match opt_path(ssot_db) {
        Ok(v) => v,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let timeout = if timeout_ms == 0 {
        DEFAULT_TIMEOUT
    } else {
        Duration::from_millis(timeout_ms as u64)
    };
    let cwd_ref = cwd.as_ref().map(Path::new);
    let db_ref = db.as_ref().map(Path::new);
    match run_tool_sandboxed(
        Path::new(root),
        program,
        &args,
        cwd_ref,
        timeout,
        None,
        db_ref,
    ) {
        Ok(report) => match serde_json::to_string(&report) {
            Ok(json) => write_out(&json, out, out_len),
            Err(e) => {
                set_err(&e.to_string());
                -1
            }
        },
        Err(e) => {
            set_err(&e);
            -1
        }
    }
}

/// ctypes stand-in for the requested PyO3 method
/// `run_test_suite(cmd: &str, working_dir: &str, timeout_secs: u64) -> TestResult`.
#[no_mangle]
pub extern "C" fn robber_run_test_suite(
    root: *const c_char,
    cmd: *const c_char,
    working_dir: *const c_char,
    timeout_secs: u32,
    ssot_db: *const c_char,
    out: *mut c_char,
    out_len: u32,
) -> c_int {
    let root = match c_str(root) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let cmd = match c_str(cmd) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let cwd = match opt_path(working_dir) {
        Ok(v) => v,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let db = match opt_path(ssot_db) {
        Ok(v) => v,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let secs = if timeout_secs == 0 {
        DEFAULT_TEST_TIMEOUT.as_secs()
    } else {
        timeout_secs as u64
    };
    let cwd_ref = cwd.as_deref().unwrap_or(root);
    match run_test_suite(
        Path::new(root),
        cmd,
        cwd_ref,
        secs,
        db.as_ref().map(Path::new),
    ) {
        Ok(report) => match serde_json::to_string(&report) {
            Ok(json) => write_out(&json, out, out_len),
            Err(e) => {
                set_err(&e.to_string());
                -1
            }
        },
        Err(e) => {
            set_err(&e);
            -1
        }
    }
}

/// Apply a unified diff across files with tree-sitter syntax abort + rollback.
#[no_mangle]
pub extern "C" fn robber_apply_diff_atomic(
    root: *const c_char,
    diff: *const c_char,
    ssot_db: *const c_char,
    out: *mut c_char,
    out_len: u32,
) -> c_int {
    let root = match c_str(root) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let diff = match c_str(diff) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let db = match opt_path(ssot_db) {
        Ok(v) => v,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    match apply_diff_atomic(Path::new(root), diff, db.as_ref().map(Path::new)) {
        Ok(report) => match serde_json::to_string(&report) {
            Ok(json) => write_out(&json, out, out_len),
            Err(e) => {
                set_err(&e.to_string());
                -1
            }
        },
        Err(e) => {
            set_err(&e);
            -1
        }
    }
}

/// mmap regex search (same engine as `robber_mmap_search`).
#[no_mangle]
pub extern "C" fn robber_quick_search(
    root: *const c_char,
    pattern: *const c_char,
    max_hits: u32,
    out: *mut c_char,
    out_len: u32,
) -> c_int {
    let root = match c_str(root) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let pattern = match c_str(pattern) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let cap = if max_hits == 0 { 64 } else { max_hits as usize };
    match quick_search(Path::new(root), pattern, cap) {
        Ok(hits) => match serde_json::to_string(&hits) {
            Ok(json) => write_out(&json, out, out_len),
            Err(e) => {
                set_err(&e.to_string());
                -1
            }
        },
        Err(e) => {
            set_err(&e);
            -1
        }
    }
}

/// ctypes stand-in for the requested PyO3 method
/// `get_symbol_context(query_symbol: &str, depth: usize) -> String`.
#[no_mangle]
pub extern "C" fn robber_get_symbol_context(
    root: *const c_char,
    query_symbol: *const c_char,
    depth: u32,
    out: *mut c_char,
    out_len: u32,
) -> c_int {
    let root = match c_str(root) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let query = match c_str(query_symbol) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let json = crate::Indexer::new(root).get_symbol_context(query, depth as usize);
    write_out(&json, out, out_len)
}

/// ctypes stand-in for PyO3 `ast_merge(base, ours, theirs) -> Result<String, ConflictError>`.
#[no_mangle]
pub extern "C" fn robber_ast_merge(
    base: *const c_char,
    ours: *const c_char,
    theirs: *const c_char,
    out: *mut c_char,
    out_len: u32,
) -> c_int {
    let base = match c_str(base) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let ours = match c_str(ours) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let theirs = match c_str(theirs) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    match crate::git_engine::ast_merge(base, ours, theirs) {
        Ok(merged) => {
            let json = serde_json::json!({"ok": true, "merged": merged}).to_string();
            write_out(&json, out, out_len)
        }
        Err(err) => {
            let json = serde_json::json!({
                "ok": false,
                "symbol": err.symbol,
                "error": err.detail,
            })
            .to_string();
            write_out(&json, out, out_len)
        }
    }
}

#[no_mangle]
pub extern "C" fn robber_commit_message(
    base: *const c_char,
    ours: *const c_char,
    out: *mut c_char,
    out_len: u32,
) -> c_int {
    let base = match c_str(base) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let ours = match c_str(ours) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let msg = crate::git_engine::commit_message(base, ours);
    write_out(&msg, out, out_len)
}

#[no_mangle]
pub extern "C" fn robber_feature_branch_name(
    task_id: *const c_char,
    slug: *const c_char,
    out: *mut c_char,
    out_len: u32,
) -> c_int {
    let task_id = match c_str(task_id) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let slug = match c_str(slug) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let name = crate::git_engine::feature_branch_name(task_id, slug);
    write_out(&name, out, out_len)
}

#[no_mangle]
pub extern "C" fn robber_conventional_commit(
    base: *const c_char,
    ours: *const c_char,
    paths: *const c_char,
    scope: *const c_char,
    out: *mut c_char,
    out_len: u32,
) -> c_int {
    let base = match c_str(base) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let ours = match c_str(ours) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let path_blob = match c_str(paths) {
        Ok(s) => s,
        Err(_) => "",
    };
    let scope = match c_str(scope) {
        Ok(s) => s,
        Err(_) => "",
    };
    let path_list: Vec<&str> = path_blob
        .split(['\n', ';', ','])
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .collect();
    let msg = crate::git_engine::conventional_commit(base, ours, &path_list, scope);
    write_out(&msg, out, out_len)
}

#[no_mangle]
pub extern "C" fn robber_security_audit(
    source: *const c_char,
    path: *const c_char,
    out: *mut c_char,
    out_len: u32,
) -> c_int {
    let source = match c_str(source) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let path = match opt_path(path) {
        Ok(v) => v.unwrap_or_default(),
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let findings = crate::security_audit::audit_source(source, &path);
    match serde_json::to_string(&findings) {
        Ok(json) => write_out(&json, out, out_len),
        Err(e) => {
            set_err(&e.to_string());
            -1
        }
    }
}

#[no_mangle]
pub extern "C" fn robber_search_symbols(
    root: *const c_char,
    query: *const c_char,
    lang_filter: *const c_char,
    out: *mut c_char,
    out_len: u32,
) -> c_int {
    let root = match c_str(root) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let query = match c_str(query) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let lang = match opt_path(lang_filter) {
        Ok(v) => v,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let hits = crate::symbol_search::search_workspace_symbols(
        Path::new(root),
        query,
        lang.as_deref(),
    );
    match serde_json::to_string(&hits) {
        Ok(json) => write_out(&json, out, out_len),
        Err(e) => {
            set_err(&e.to_string());
            -1
        }
    }
}

#[no_mangle]
pub extern "C" fn robber_find_references(
    root: *const c_char,
    symbol_name: *const c_char,
    out: *mut c_char,
    out_len: u32,
) -> c_int {
    let root = match c_str(root) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let symbol = match c_str(symbol_name) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let graph = crate::symbol_search::find_symbol_references(
        Path::new(root),
        symbol,
    );
    match serde_json::to_string(&graph) {
        Ok(json) => write_out(&json, out, out_len),
        Err(e) => {
            set_err(&e.to_string());
            -1
        }
    }
}

#[no_mangle]
pub extern "C" fn robber_replace_ast_node(
    root: *const c_char,
    file_path: *const c_char,
    target_symbol: *const c_char,
    new_code: *const c_char,
    out: *mut c_char,
    out_len: u32,
) -> c_int {
    let root = match c_str(root) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let file = match c_str(file_path) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let sym = match c_str(target_symbol) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    let code = match c_str(new_code) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    match crate::symbol_search::replace_ast_node(Path::new(root), file, sym, code) {
        Ok(updated) => {
            let json = serde_json::json!({"ok": true, "updated": updated}).to_string();
            write_out(&json, out, out_len)
        }
        Err(err) => {
            let json = serde_json::json!({"ok": false, "error": err.message, "kind": err.kind}).to_string();
            write_out(&json, out, out_len)
        }
    }
}
