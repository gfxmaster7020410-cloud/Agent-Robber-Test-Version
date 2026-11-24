//! C ABI so Python can load the indexer without PyO3 at compile time.

use std::cell::RefCell;
use std::ffi::{CStr, CString};
use std::os::raw::{c_char, c_int};
use std::path::Path;
use std::ptr;

use crate::{Indexer, IndexReport};

thread_local! {
    static LAST_ERR: RefCell<Option<CString>> = RefCell::new(None);
}

pub(crate) fn set_err(msg: &str) {
    LAST_ERR.with(|slot| {
        *slot.borrow_mut() = CString::new(msg.replace('\0', " ")).ok();
    });
}

pub(crate) fn c_str<'a>(ptr: *const c_char) -> Result<&'a str, String> {
    if ptr.is_null() {
        return Err("null pointer".into());
    }
    unsafe { CStr::from_ptr(ptr) }
        .to_str()
        .map_err(|e| e.to_string())
}

pub(crate) fn write_out(json: &str, out: *mut c_char, out_len: u32) -> c_int {
    if out.is_null() || out_len == 0 {
        return -2;
    }
    let bytes = json.as_bytes();
    let n = (out_len as usize).saturating_sub(1);
    if bytes.len() > n {
        set_err("output buffer too small");
        return -3;
    }
    unsafe {
        ptr::copy_nonoverlapping(bytes.as_ptr(), out as *mut u8, bytes.len());
        *out.add(bytes.len()) = 0;
    }
    bytes.len() as c_int
}

#[no_mangle]
pub extern "C" fn robber_last_error() -> *const c_char {
    LAST_ERR.with(|slot| match slot.borrow().as_ref() {
        Some(s) => s.as_ptr(),
        None => ptr::null(),
    })
}

/// Index `root` into `ssot_db`. Writes a JSON report into `out`.
#[no_mangle]
pub extern "C" fn robber_index_workspace(
    root: *const c_char,
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
    let db = match c_str(ssot_db) {
        Ok(s) => s,
        Err(e) => {
            set_err(&e);
            return -1;
        }
    };
    match Indexer::new(root).index_into(Path::new(db)) {
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

/// Regex search under `root`. JSON array of hits written to `out`.
#[no_mangle]
pub extern "C" fn robber_mmap_search(
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
    match Indexer::new(root).search(pattern, cap) {
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

/// Drop thread-local error state and ask bundled SQLite to give heap back.
#[no_mangle]
pub extern "C" fn robber_release_caches() -> c_int {
    LAST_ERR.with(|slot| {
        *slot.borrow_mut() = None;
    });
    if let Ok(conn) = rusqlite::Connection::open_in_memory() {
        let _ = conn.execute_batch("PRAGMA shrink_memory;");
    }
    0
}

#[allow(dead_code)]
fn _report_shape(report: &IndexReport) -> usize {
    report.files_parsed
}
