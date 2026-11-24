//! Integration coverage for `cargo test` spawning the real `tools` binary.

use std::path::PathBuf;
use std::process::Command;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

fn scratch(tag: &str) -> PathBuf {
    let n = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dir = std::env::temp_dir().join(format!("robber-tools-it-{tag}-{n}"));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn tools_bin_kills_runaway_alloc() {
    let dir = scratch("alloc");
    let bin = env!("CARGO_BIN_EXE_tools");
    let started = Instant::now();
    let nested = Command::new(bin)
        .args([
            "run",
            "--root",
            dir.to_str().unwrap(),
            "--timeout-ms",
            "8000",
            "--",
            bin,
            "__alloc",
        ])
        .output()
        .expect("spawn tools run");
    let elapsed = started.elapsed();
    assert!(
        elapsed < Duration::from_secs(15),
        "runaway alloc was not cut off ({elapsed:?})"
    );
    let stdout = String::from_utf8_lossy(&nested.stdout);
    assert!(
        stdout.contains("\"killed\":true")
            || stdout.contains("\"memory_exceeded\":true")
            || stdout.contains("\"timed_out\":true"),
        "expected sandbox kill flags in {stdout}"
    );
    std::fs::remove_dir_all(&dir).ok();
}
