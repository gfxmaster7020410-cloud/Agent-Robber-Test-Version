//! Tool-engine CLI. `cargo test --bin tools` runs the isolation suite.

use std::env;
use std::process::ExitCode;
use std::time::Duration;

use robber_indexer::tools::{
    apply_diff_atomic, quick_search, run_tool_sandboxed, runaway_alloc, DEFAULT_TIMEOUT,
};

fn flag(args: &[String], name: &str) -> Option<String> {
    args.windows(2).find_map(|w| {
        if w[0] == name {
            Some(w[1].clone())
        } else {
            None
        }
    })
}

fn usage() -> ExitCode {
    eprintln!(
        "tools run --root PATH [--ssot PATH] [--timeout-ms N] -- PROGRAM [ARGS...]\ntools patch --root PATH --diff FILE [--ssot PATH]\ntools search --root PATH --pattern REGEX"
    );
    ExitCode::from(2)
}

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    let cmd = args.first().map(String::as_str).unwrap_or("");
    match cmd {
        "__alloc" => runaway_alloc(),
        "run" => {
            let root = flag(&args, "--root").unwrap_or_else(|| ".".into());
            let ssot = flag(&args, "--ssot");
            let timeout_ms = flag(&args, "--timeout-ms")
                .and_then(|s| s.parse().ok())
                .unwrap_or(DEFAULT_TIMEOUT.as_millis() as u64);
            let split = args.iter().position(|a| a == "--");
            let (program, rest) = match split {
                Some(i) => {
                    let tail = &args[i + 1..];
                    if tail.is_empty() {
                        return usage();
                    }
                    (tail[0].clone(), tail[1..].to_vec())
                }
                None => return usage(),
            };
            match run_tool_sandboxed(
                std::path::Path::new(&root),
                &program,
                &rest,
                None,
                Duration::from_millis(timeout_ms),
                None,
                ssot.as_ref().map(std::path::Path::new),
            ) {
                Ok(report) => {
                    println!(
                        "{}",
                        serde_json::to_string(&report).unwrap_or_else(|_| "{}".into())
                    );
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    eprintln!("{e}");
                    ExitCode::from(1)
                }
            }
        }
        "patch" => {
            let root = flag(&args, "--root").unwrap_or_else(|| ".".into());
            let diff_path = match flag(&args, "--diff") {
                Some(p) => p,
                None => return usage(),
            };
            let ssot = flag(&args, "--ssot");
            let diff = match std::fs::read_to_string(&diff_path) {
                Ok(s) => s,
                Err(e) => {
                    eprintln!("{e}");
                    return ExitCode::from(1);
                }
            };
            match apply_diff_atomic(
                std::path::Path::new(&root),
                &diff,
                ssot.as_ref().map(std::path::Path::new),
            ) {
                Ok(report) => {
                    println!(
                        "{}",
                        serde_json::to_string(&report).unwrap_or_else(|_| "{}".into())
                    );
                    if report.ok {
                        ExitCode::SUCCESS
                    } else {
                        ExitCode::from(1)
                    }
                }
                Err(e) => {
                    eprintln!("{e}");
                    ExitCode::from(1)
                }
            }
        }
        "search" => {
            let root = flag(&args, "--root").unwrap_or_else(|| ".".into());
            let pattern = match flag(&args, "--pattern") {
                Some(p) => p,
                None => return usage(),
            };
            match quick_search(std::path::Path::new(&root), &pattern, 64) {
                Ok(hits) => {
                    println!(
                        "{}",
                        serde_json::to_string(&hits).unwrap_or_else(|_| "[]".into())
                    );
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    eprintln!("{e}");
                    ExitCode::from(1)
                }
            }
        }
        _ => usage(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::mpsc;
    use std::time::{SystemTime, UNIX_EPOCH};

    use robber_indexer::tools::{
        AtomicDiffPatcher, CommandSpec, ExecutionLogger, SandboxedRunner, DEFAULT_MEMORY_CAP,
    };

    fn scratch(tag: &str) -> PathBuf {
        let n = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("robber-tools-{tag}-{n}"));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn echo_spec(root: &Path, msg: &str) -> CommandSpec {
        #[cfg(windows)]
        {
            CommandSpec {
                program: PathBuf::from("cmd"),
                args: vec!["/C".into(), format!("echo {msg}")],
                cwd: root.to_path_buf(),
                shell: false,
            }
        }
        #[cfg(not(windows))]
        {
            CommandSpec {
                program: PathBuf::from("sh"),
                args: vec!["-c".into(), format!("echo {msg}")],
                cwd: root.to_path_buf(),
                shell: false,
            }
        }
    }

    fn look_path(name: &str) -> Option<PathBuf> {
        let path = env::var_os("PATH")?;
        for dir in env::split_paths(&path) {
            let cand = dir.join(name);
            if cand.is_file() {
                return Some(cand);
            }
            #[cfg(windows)]
            {
                let exe = dir.join(format!("{name}.exe"));
                if exe.is_file() {
                    return Some(exe);
                }
            }
        }
        None
    }

    #[test]
    fn multi_file_diff_then_syntax_rollback() {
        let dir = scratch("patch");
        fs::write(dir.join("a.py"), "def a():\n    return 1\n").unwrap();
        fs::write(dir.join("b.py"), "def b():\n    return 2\n").unwrap();
        let db = dir.join("ssot.db");
        let report = apply_diff_atomic(
            &dir,
            "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n def a():\n-    return 1\n+    return 10\n--- a/b.py\n+++ b/b.py\n@@ -1,2 +1,2 @@\n def b():\n-    return 2\n+    return 20\n",
            Some(&db),
        )
        .unwrap();
        assert!(report.ok, "{}", report.error);
        assert_eq!(report.files.len(), 2);
        assert!(!report.artifact_id.is_empty());
        assert!(fs::read_to_string(dir.join("a.py"))
            .unwrap()
            .contains("return 10"));
        assert!(fs::read_to_string(dir.join("b.py"))
            .unwrap()
            .contains("return 20"));

        let bad = AtomicDiffPatcher::new(&dir)
            .apply("--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,1 @@\n-def a():\n-    return 10\n+def a(\n")
            .unwrap();
        assert!(!bad.ok);
        assert!(bad.rolled_back);
        assert!(fs::read_to_string(dir.join("a.py"))
            .unwrap()
            .contains("return 10"));
        assert!(fs::read_to_string(dir.join("b.py"))
            .unwrap()
            .contains("return 20"));
        let log = ExecutionLogger::connect(&db, &dir).unwrap();
        assert!(log.count().unwrap() >= 1);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn runaway_hits_memory_cap_and_stops() {
        let dir = scratch("mem");
        let python = look_path("python")
            .or_else(|| look_path("python3"))
            .or_else(|| look_path("py"))
            .filter(|p| {
                !p.to_string_lossy()
                    .to_ascii_lowercase()
                    .contains("windowsapps")
            });
        let spec = {
            #[cfg(windows)]
            {
                let _ = python;
                CommandSpec {
                    program: PathBuf::from("powershell"),
                    args: vec![
                        "-NoProfile".into(),
                        "-Command".into(),
                        "$x = New-Object byte[] 314572800; Start-Sleep -Seconds 20".into(),
                    ],
                    cwd: dir.clone(),
                    shell: false,
                }
            }
            #[cfg(not(windows))]
            {
                if let Some(py) = python {
                    CommandSpec {
                        program: py,
                        args: vec![
                            "-c".into(),
                            "x=bytearray(300*1024*1024); import time; time.sleep(20)".into(),
                        ],
                        cwd: dir.clone(),
                        shell: false,
                    }
                } else {
                    CommandSpec {
                        program: PathBuf::from("sh"),
                        args: vec![
                            "-c".into(),
                            "python3 -c 'x=bytearray(300*1024*1024); import time; time.sleep(20)'"
                                .into(),
                        ],
                        cwd: dir.clone(),
                        shell: false,
                    }
                }
            }
        };
        let runner = SandboxedRunner::new(&dir);
        let started = std::time::Instant::now();
        let result = runner.run(spec, None).unwrap();
        let elapsed = started.elapsed();
        assert!(
            elapsed.as_secs() < 15,
            "runaway was not terminated in time ({:?})",
            elapsed
        );
        assert!(
            result.killed || result.memory_exceeded || result.timed_out,
            "expected sandbox kill, got {:?}",
            result
        );
        assert!(DEFAULT_MEMORY_CAP >= 250 * 1024 * 1024);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn streams_echo_and_logs_artifact() {
        let dir = scratch("echo");
        let db = dir.join("ssot.db");
        let (tx, rx) = mpsc::channel();
        let runner = SandboxedRunner::new(&dir);
        let result = runner.run(echo_spec(&dir, "robber-ok"), Some(tx)).unwrap();
        let chunks: Vec<_> = rx.try_iter().collect();
        assert!(
            result.stdout.contains("robber-ok")
                || chunks
                    .iter()
                    .any(|c| String::from_utf8_lossy(&c.data).contains("robber-ok")),
            "stdout={} stderr={}",
            result.stdout,
            result.stderr
        );
        let mut log = ExecutionLogger::connect(&db, &dir).unwrap();
        log.log_tool(&result, "echo", "rules").unwrap();
        assert_eq!(log.count().unwrap(), 1);
        fs::write(dir.join("hit.py"), "def robber_marker():\n    return 1\n").unwrap();
        assert!(!quick_search(&dir, "robber_marker", 8).unwrap().is_empty());
        fs::remove_dir_all(&dir).ok();
    }
}
