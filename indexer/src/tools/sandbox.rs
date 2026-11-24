//! Cross-platform process isolation with a 250MB cap, timeout, and streamed IO.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Sender};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;

use super::confined_join;

pub const DEFAULT_MEMORY_CAP: u64 = 250 * 1024 * 1024;
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(8);
pub const DEFAULT_TEST_TIMEOUT: Duration = Duration::from_secs(30);
const TEST_LOG_CAP: usize = 32 * 1024;

#[derive(Clone, Debug)]
pub struct SandboxLimits {
    pub memory_bytes: u64,
    pub timeout: Duration,
    pub stdout_max: usize,
}

impl Default for SandboxLimits {
    fn default() -> Self {
        Self {
            memory_bytes: DEFAULT_MEMORY_CAP,
            timeout: DEFAULT_TIMEOUT,
            stdout_max: 1024 * 1024,
        }
    }
}

#[derive(Clone, Debug)]
pub struct CommandSpec {
    pub program: PathBuf,
    pub args: Vec<String>,
    pub cwd: PathBuf,
    pub shell: bool,
}

#[derive(Clone, Debug)]
pub struct StreamChunk {
    pub stream: String,
    pub data: Vec<u8>,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct ToolResult {
    pub exit_code: Option<i32>,
    pub stdout: String,
    pub stderr: String,
    pub elapsed_ms: u128,
    pub killed: bool,
    pub timed_out: bool,
    pub memory_exceeded: bool,
    pub kill_reason: String,
}

/// Structured result of `run_test_suite` (pytest / go test / cargo test / jest).
#[derive(Clone, Debug, Default, Serialize)]
pub struct TestResult {
    pub ok: bool,
    pub passed: u32,
    pub failed: u32,
    pub skipped: u32,
    pub exit_code: Option<i32>,
    pub stdout: String,
    pub stderr: String,
    pub elapsed_ms: u128,
    pub timed_out: bool,
    pub memory_exceeded: bool,
    pub killed: bool,
    pub kill_reason: String,
    pub command: String,
    pub working_dir: String,
    pub timeout_secs: u64,
    pub artifact_id: String,
    pub runner: String,
    pub memory_cap_bytes: u64,
}

pub struct SandboxedRunner {
    root: PathBuf,
    limits: SandboxLimits,
}

impl SandboxedRunner {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self {
            root: root.into(),
            limits: SandboxLimits::default(),
        }
    }

    pub fn with_limits(mut self, limits: SandboxLimits) -> Self {
        self.limits = limits;
        self
    }

    pub fn run(
        &self,
        spec: CommandSpec,
        stream: Option<Sender<StreamChunk>>,
    ) -> Result<ToolResult, String> {
        let root = std::fs::canonicalize(&self.root).unwrap_or_else(|_| self.root.clone());
        let cwd = confine_cwd(&root, &spec.cwd)?;
        let program = resolve_program(&root, &spec.program)?;
        if program
            .file_name()
            .and_then(|n| n.to_str())
            .map(|n| crate::crawl::SECRET_NAMES.contains(&n))
            .unwrap_or(false)
        {
            return Err("refusing protected program".into());
        }

        let mut cmd = if spec.shell {
            shell_command(&program, &spec.args)?
        } else {
            let mut c = Command::new(&program);
            c.args(&spec.args);
            c
        };
        cmd.current_dir(&cwd)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        cmd.env("ROBBER_WORKSPACE", &root);
        cmd.env("ROBBER_WORKSPACE_ROOT", &root);
        strip_secret_env(&mut cmd);

        #[cfg(unix)]
        apply_unix_isolation(&mut cmd, &self.limits);

        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x08000000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }

        let started = Instant::now();
        let mut child = cmd.spawn().map_err(|e| format!("spawn failed: {e}"))?;

        #[cfg(windows)]
        let proc_handle = {
            use std::os::windows::io::AsRawHandle;
            child.as_raw_handle() as *mut core::ffi::c_void
        };
        #[cfg(windows)]
        let job = windows::assign_job(proc_handle, self.limits.memory_bytes, self.limits.timeout);

        let (tx, rx) = mpsc::channel::<StreamChunk>();
        let cap = Arc::new(AtomicU64::new(0));
        let max = self.limits.stdout_max as u64;
        let stdout = child.stdout.take();
        let stderr = child.stderr.take();
        let t_out = stdout.map(|pipe| {
            spawn_pump("stdout", pipe, tx.clone(), stream.clone(), cap.clone(), max)
        });
        let t_err = stderr.map(|pipe| {
            spawn_pump("stderr", pipe, tx.clone(), stream.clone(), cap.clone(), max)
        });
        drop(tx);

        let mut timed_out = false;
        let mut memory_exceeded = false;
        let mut killed = false;
        let deadline = started + self.limits.timeout;
        let mem_cap = self.limits.memory_bytes;
        let pid = child.id();
        #[cfg(windows)]
        let handle_usize = proc_handle as usize;
        #[cfg(not(windows))]
        let handle_usize = 0usize;

        loop {
            if Instant::now() >= deadline {
                timed_out = true;
                killed = true;
                terminate_child(&mut child, pid);
                #[cfg(windows)]
                if let Some(ref job) = job {
                    job.terminate();
                }
                break;
            }
            let rss = child_rss(pid, handle_usize);
            if rss > mem_cap && rss > 0 {
                memory_exceeded = true;
                killed = true;
                terminate_child(&mut child, pid);
                #[cfg(windows)]
                if let Some(ref job) = job {
                    job.terminate();
                }
                break;
            }
            match child.try_wait() {
                Ok(Some(_)) => break,
                Ok(None) => thread::sleep(Duration::from_millis(20)),
                Err(e) => return Err(format!("wait failed: {e}")),
            }
        }
        let status = child.wait().ok();
        if let Some(t) = t_out {
            let _ = t.join();
        }
        if let Some(t) = t_err {
            let _ = t.join();
        }

        let mut stdout_buf = String::new();
        let mut stderr_buf = String::new();
        while let Ok(chunk) = rx.try_recv() {
            let text = String::from_utf8_lossy(&chunk.data);
            if chunk.stream == "stderr" {
                stderr_buf.push_str(&text);
            } else {
                stdout_buf.push_str(&text);
            }
        }

        let exit_code = status.and_then(|s| s.code());
        let oom = looks_like_oom(exit_code, &stderr_buf);
        if oom && !memory_exceeded {
            memory_exceeded = true;
            killed = true;
        }
        let kill_reason = if memory_exceeded {
            "memory"
        } else if timed_out {
            "timeout"
        } else {
            ""
        }
        .to_string();

        #[cfg(windows)]
        drop(job);

        Ok(ToolResult {
            exit_code,
            stdout: stdout_buf,
            stderr: stderr_buf,
            elapsed_ms: started.elapsed().as_millis(),
            killed,
            timed_out,
            memory_exceeded,
            kill_reason,
        })
    }
}

/// Run a test command line under process isolation, 250 MB RAM, and a job timeout.
pub fn run_test_suite(
    root: &Path,
    cmd: &str,
    working_dir: &str,
    timeout_secs: u64,
) -> Result<TestResult, String> {
    let secs = if timeout_secs == 0 {
        DEFAULT_TEST_TIMEOUT.as_secs()
    } else {
        timeout_secs
    };
    let (program, args) = split_cmdline(cmd)?;
    let cwd = if working_dir.trim().is_empty() {
        root.to_path_buf()
    } else {
        PathBuf::from(working_dir)
    };
    let runner = SandboxedRunner::new(root).with_limits(SandboxLimits {
        memory_bytes: DEFAULT_MEMORY_CAP,
        timeout: Duration::from_secs(secs),
        stdout_max: 1024 * 1024,
    });
    let spec = CommandSpec {
        program: PathBuf::from(program),
        args,
        cwd: cwd.clone(),
        shell: false,
    };
    let result = runner.run(spec, None)?;
    let blob = format!("{}\n{}", result.stdout, result.stderr);
    let (passed, failed, skipped) = tally_results(&blob);
    let ok = result.exit_code == Some(0)
        && !result.timed_out
        && !result.memory_exceeded
        && !result.killed;
    Ok(TestResult {
        ok,
        passed,
        failed: if failed == 0 && !ok { 1 } else { failed },
        skipped,
        exit_code: result.exit_code,
        stdout: clip_text(&result.stdout, TEST_LOG_CAP),
        stderr: clip_text(&result.stderr, TEST_LOG_CAP),
        elapsed_ms: result.elapsed_ms,
        timed_out: result.timed_out,
        memory_exceeded: result.memory_exceeded,
        killed: result.killed,
        kill_reason: result.kill_reason,
        command: clip_text(cmd, 1024),
        working_dir: cwd.to_string_lossy().into_owned(),
        timeout_secs: secs,
        artifact_id: String::new(),
        runner: infer_runner(cmd),
        memory_cap_bytes: DEFAULT_MEMORY_CAP,
    })
}

pub(crate) fn split_cmdline(cmd: &str) -> Result<(String, Vec<String>), String> {
    let mut parts: Vec<String> = Vec::new();
    let mut cur = String::new();
    let mut quote: Option<char> = None;
    for c in cmd.chars() {
        if let Some(q) = quote {
            if c == q {
                quote = None;
            } else {
                cur.push(c);
            }
            continue;
        }
        if c == '"' || c == '\'' {
            quote = Some(c);
            continue;
        }
        if c.is_whitespace() {
            if !cur.is_empty() {
                parts.push(std::mem::take(&mut cur));
            }
            continue;
        }
        cur.push(c);
    }
    if quote.is_some() {
        return Err("unclosed quote in command".into());
    }
    if !cur.is_empty() {
        parts.push(cur);
    }
    let program = parts
        .first()
        .cloned()
        .ok_or_else(|| "empty command".to_string())?;
    Ok((program, parts.into_iter().skip(1).collect()))
}

fn infer_runner(cmd: &str) -> String {
    let lower = cmd.to_ascii_lowercase();
    if lower.contains("pytest") {
        "pytest".into()
    } else if lower.contains("cargo test") {
        "cargo".into()
    } else if lower.contains("jest") {
        "jest".into()
    } else if lower.contains("go test") {
        "go".into()
    } else {
        "generic".into()
    }
}

fn tally_results(blob: &str) -> (u32, u32, u32) {
    let lower = blob.to_ascii_lowercase();
    let bytes = lower.as_bytes();
    let mut passed = 0u32;
    let mut failed = 0u32;
    let mut skipped = 0u32;
    let mut i = 0usize;
    while i < bytes.len() {
        if bytes[i].is_ascii_digit() {
            let start = i;
            while i < bytes.len() && bytes[i].is_ascii_digit() {
                i += 1;
            }
            let n: u32 = lower[start..i].parse().unwrap_or(0);
            while i < bytes.len() && bytes[i] == b' ' {
                i += 1;
            }
            let rest = &lower[i..];
            if rest.starts_with("passed") {
                passed = passed.saturating_add(n);
            } else if rest.starts_with("failed") || rest.starts_with("error") {
                failed = failed.saturating_add(n);
            } else if rest.starts_with("skipped") {
                skipped = skipped.saturating_add(n);
            }
            continue;
        }
        i += 1;
    }
    (passed, failed, skipped)
}

fn clip_text(text: &str, max: usize) -> String {
    let clean = text.replace('\0', "");
    if clean.len() <= max {
        return clean;
    }
    let mut end = max;
    while end > 0 && !clean.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}\n[truncated]", &clean[..end])
}

/// Allocate past the 250MB cap. Used by `tools __alloc` for isolation tests.
pub fn runaway_alloc() -> ! {
    let mut chunks: Vec<Vec<u8>> = Vec::new();
    loop {
        chunks.push(vec![1u8; 4 * 1024 * 1024]);
        if chunks.len() > 512 {
            thread::sleep(Duration::from_secs(30));
        }
    }
}

fn confine_cwd(root: &Path, cwd: &Path) -> Result<PathBuf, String> {
    let joined = if cwd.is_absolute() {
        cwd.to_path_buf()
    } else {
        confined_join(root, &cwd.to_string_lossy())?
    };
    let canon = std::fs::canonicalize(&joined).unwrap_or(joined);
    let root_c = std::fs::canonicalize(root).unwrap_or_else(|_| root.to_path_buf());
    if !canon.starts_with(&root_c) {
        return Err("working directory escapes workspace".into());
    }
    if !canon.is_dir() {
        return Err("working directory is not a directory".into());
    }
    Ok(canon)
}

fn resolve_program(root: &Path, program: &Path) -> Result<PathBuf, String> {
    let raw = program.to_string_lossy();
    if raw.trim().is_empty() {
        return Err("empty program".into());
    }
    if raw.contains('/') || raw.contains('\\') || program.is_absolute() {
        if program.is_absolute() {
            if program.is_file() {
                return Ok(program.to_path_buf());
            }
            return Err(format!("program not found: {raw}"));
        }
        let p = confined_join(root, &raw)?;
        if p.is_file() {
            return Ok(p);
        }
        return Err(format!("program not found: {raw}"));
    }
    look_path(&raw).ok_or_else(|| format!("program not found on PATH: {raw}"))
}

fn look_path(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
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
            let cmd = dir.join(format!("{name}.cmd"));
            if cmd.is_file() {
                return Some(cmd);
            }
        }
    }
    #[cfg(windows)]
    {
        if name.eq_ignore_ascii_case("cmd") {
            if let Some(comspec) = std::env::var_os("ComSpec") {
                let p = PathBuf::from(comspec);
                if p.is_file() {
                    return Some(p);
                }
            }
        }
    }
    None
}

fn shell_command(program: &Path, args: &[String]) -> Result<Command, String> {
    let line = if args.is_empty() {
        program.to_string_lossy().into_owned()
    } else {
        format!("{} {}", program.display(), args.join(" "))
    };
    if line.len() > 8 * 1024 {
        return Err("command line too long".into());
    }
    #[cfg(windows)]
    {
        let mut c = Command::new(std::env::var("ComSpec").unwrap_or_else(|_| "cmd.exe".into()));
        c.args(["/C", &line]);
        Ok(c)
    }
    #[cfg(not(windows))]
    {
        let mut c = Command::new("sh");
        c.args(["-c", &line]);
        Ok(c)
    }
}

fn strip_secret_env(cmd: &mut Command) {
    let keys: Vec<String> = std::env::vars()
        .filter(|(k, _)| is_secret_env(k))
        .map(|(k, _)| k)
        .collect();
    for k in keys {
        cmd.env_remove(k);
    }
}

fn is_secret_env(key: &str) -> bool {
    let u = key.to_ascii_uppercase();
    if matches!(
        u.as_str(),
        "PATH"
            | "PATHEXT"
            | "SYSTEMROOT"
            | "WINDIR"
            | "COMSPEC"
            | "HOME"
            | "USERPROFILE"
            | "TEMP"
            | "TMP"
            | "USERNAME"
            | "USER"
            | "SHELL"
            | "LANG"
            | "LC_ALL"
            | "SYSTEMDRIVE"
            | "NUMBER_OF_PROCESSORS"
            | "PROCESSOR_ARCHITECTURE"
    ) {
        return false;
    }
    u.contains("SECRET")
        || u.contains("PASSWORD")
        || u.contains("TOKEN")
        || u.contains("CREDENTIAL")
        || (u.contains("API") && u.contains("KEY"))
        || u.ends_with("_KEY")
}

fn spawn_pump(
    label: &'static str,
    mut pipe: impl Read + Send + 'static,
    tx: Sender<StreamChunk>,
    extra: Option<Sender<StreamChunk>>,
    cap: Arc<AtomicU64>,
    max: u64,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let mut buf = [0u8; 4096];
        loop {
            match pipe.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    let used = cap.fetch_add(n as u64, Ordering::Relaxed);
                    if used >= max {
                        continue;
                    }
                    let take = n.min((max - used) as usize);
                    let data = buf[..take].to_vec();
                    let chunk = StreamChunk {
                        stream: label.to_string(),
                        data,
                    };
                    if let Some(ref extra) = extra {
                        let _ = extra.send(chunk.clone());
                    }
                    let _ = tx.send(chunk);
                }
                Err(_) => break,
            }
        }
    })
}

fn looks_like_oom(exit: Option<i32>, stderr: &str) -> bool {
    if stderr.to_ascii_lowercase().contains("memory allocation") {
        return true;
    }
    match exit {
        Some(code) if code == 137 || code == 3 => true,
        Some(code) if code < 0 => {
            let nt = code as u32;
            // STATUS_NO_MEMORY, STATUS_COMMITMENT_LIMIT, STATUS_STACK_BUFFER_OVERRUN (CRT abort)
            matches!(nt, 0xC0000017 | 0xC000012D | 0xC0000409 | 0xC0000374)
        }
        _ => false,
    }
}

fn terminate_child(child: &mut std::process::Child, pid: u32) {
    let _ = child.kill();
    #[cfg(unix)]
    unsafe {
        libc::kill(-(pid as i32), libc::SIGKILL);
    }
    #[cfg(not(unix))]
    let _ = pid;
}

fn child_rss(pid: u32, handle: usize) -> u64 {
    #[cfg(windows)]
    {
        let _ = pid;
        return windows::process_private_bytes(handle as *mut core::ffi::c_void);
    }
    #[cfg(target_os = "linux")]
    {
        let _ = handle;
        let text = std::fs::read_to_string(format!("/proc/{pid}/status")).unwrap_or_default();
        for line in text.lines() {
            if let Some(rest) = line.strip_prefix("VmRSS:") {
                let kb: u64 = rest
                    .split_whitespace()
                    .next()
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(0);
                return kb.saturating_mul(1024);
            }
        }
        return 0;
    }
    #[cfg(not(any(windows, target_os = "linux")))]
    {
        let _ = (pid, handle);
        0
    }
    #[allow(unreachable_code)]
    0
}

#[cfg(unix)]
fn apply_unix_isolation(cmd: &mut Command, limits: &SandboxLimits) {
    use std::os::unix::process::CommandExt;
    let mem = limits.memory_bytes;
    let cpu = limits.timeout.as_secs().max(1) as u64;
    unsafe {
        cmd.pre_exec(move || {
            let as_lim = libc::rlimit {
                rlim_cur: mem,
                rlim_max: mem,
            };
            let cpu_lim = libc::rlimit {
                rlim_cur: cpu,
                rlim_max: cpu,
            };
            let core = libc::rlimit {
                rlim_cur: 0,
                rlim_max: 0,
            };
            libc::setrlimit(libc::RLIMIT_AS, &as_lim);
            libc::setrlimit(libc::RLIMIT_CPU, &cpu_lim);
            libc::setrlimit(libc::RLIMIT_CORE, &core);
            libc::setpgid(0, 0);
            Ok(())
        });
    }
    cmd.process_group(0);
}

#[cfg(windows)]
mod windows {
    use std::mem::{size_of, zeroed};
    use std::time::Duration;

    const JOB_OBJECT_LIMIT_PROCESS_MEMORY: u32 = 0x0000_0100;
    const JOB_OBJECT_LIMIT_JOB_MEMORY: u32 = 0x0000_0200;
    const JOB_OBJECT_LIMIT_PROCESS_TIME: u32 = 0x0000_0002;
    const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x0000_2000;
    const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: i32 = 9;

    #[repr(C)]
    struct JobObjectBasicLimitInformation {
        per_process_user_time_limit: i64,
        per_job_user_time_limit: i64,
        limit_flags: u32,
        minimum_working_set_size: usize,
        maximum_working_set_size: usize,
        active_process_limit: u32,
        affinity: usize,
        priority_class: u32,
        scheduling_class: u32,
    }

    #[repr(C)]
    struct IoCounters {
        read_op: u64,
        write_op: u64,
        other_op: u64,
        read_trans: u64,
        write_trans: u64,
        other_trans: u64,
    }

    #[repr(C)]
    struct JobObjectExtendedLimitInformation {
        basic: JobObjectBasicLimitInformation,
        io: IoCounters,
        process_memory_limit: usize,
        job_memory_limit: usize,
        peak_process_memory_used: usize,
        peak_job_memory_used: usize,
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn CreateJobObjectW(attr: *mut core::ffi::c_void, name: *const u16) -> *mut core::ffi::c_void;
        fn SetInformationJobObject(
            job: *mut core::ffi::c_void,
            class: i32,
            info: *mut core::ffi::c_void,
            len: u32,
        ) -> i32;
        fn AssignProcessToJobObject(
            job: *mut core::ffi::c_void,
            process: *mut core::ffi::c_void,
        ) -> i32;
        fn TerminateJobObject(job: *mut core::ffi::c_void, exit_code: u32) -> i32;
        fn CloseHandle(handle: *mut core::ffi::c_void) -> i32;
    }

    pub struct JobGuard {
        handle: *mut core::ffi::c_void,
    }

    impl JobGuard {
        pub fn terminate(&self) {
            if !self.handle.is_null() {
                unsafe {
                    TerminateJobObject(self.handle, 1);
                }
            }
        }
    }

    impl Drop for JobGuard {
        fn drop(&mut self) {
            unsafe {
                if !self.handle.is_null() {
                    CloseHandle(self.handle);
                }
            }
        }
    }

    pub fn assign_job(
        process: *mut core::ffi::c_void,
        memory: u64,
        timeout: Duration,
    ) -> Option<JobGuard> {
        unsafe {
            let job = CreateJobObjectW(std::ptr::null_mut(), std::ptr::null());
            if job.is_null() {
                return None;
            }
            let mut info: JobObjectExtendedLimitInformation = zeroed();
            info.basic.limit_flags = JOB_OBJECT_LIMIT_PROCESS_MEMORY
                | JOB_OBJECT_LIMIT_JOB_MEMORY
                | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                | JOB_OBJECT_LIMIT_PROCESS_TIME;
            info.process_memory_limit = memory as usize;
            info.job_memory_limit = memory as usize;
            let ticks = (timeout.as_nanos() / 100) as i64;
            info.basic.per_process_user_time_limit = ticks.max(10_000_000);
            let ok = SetInformationJobObject(
                job,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                &mut info as *mut _ as *mut core::ffi::c_void,
                size_of::<JobObjectExtendedLimitInformation>() as u32,
            );
            if ok == 0 {
                CloseHandle(job);
                return None;
            }
            if process.is_null() || AssignProcessToJobObject(job, process) == 0 {
                CloseHandle(job);
                return None;
            }
            Some(JobGuard { handle: job })
        }
    }

    pub fn process_private_bytes(handle: *mut core::ffi::c_void) -> u64 {
        crate::winmem::private_bytes(handle)
    }

    #[cfg(test)]
    #[test]
    fn job_struct_layout_x64() {
        if cfg!(target_pointer_width = "64") {
            assert_eq!(size_of::<JobObjectExtendedLimitInformation>(), 144);
        }
    }
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
        let dir = std::env::temp_dir().join(format!("robber-sbx-{n}"));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn echo_streams_stdout() {
        let dir = scratch();
        let runner = SandboxedRunner::new(&dir);
        let spec = echo_spec(&dir, "robber-ok");
        let (tx, rx) = mpsc::channel();
        let result = runner.run(spec, Some(tx)).unwrap();
        let streamed: Vec<_> = rx.try_iter().collect();
        assert!(
            result.stdout.contains("robber-ok") || streamed.iter().any(|c| String::from_utf8_lossy(&c.data).contains("robber-ok")),
            "stdout={} stderr={}",
            result.stdout,
            result.stderr
        );
        assert!(!result.memory_exceeded);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn split_cmdline_keeps_quoted_program() {
        let (program, args) =
            split_cmdline(r#""C:\Prog Files\python.exe" -m pytest tests/a.py"#).unwrap();
        assert!(program.contains("python.exe"));
        assert_eq!(args, vec!["-m", "pytest", "tests/a.py"]);
    }

    #[test]
    fn run_test_suite_echo_ok() {
        let dir = scratch();
        #[cfg(windows)]
        let cmd = r#"cmd /C "echo robber-test-ok""#;
        #[cfg(not(windows))]
        let cmd = r#"sh -c "echo robber-test-ok""#;
        let result = run_test_suite(&dir, cmd, dir.to_str().unwrap_or("."), 8).unwrap();
        assert!(
            result.ok,
            "exit={:?} stdout={} stderr={}",
            result.exit_code, result.stdout, result.stderr
        );
        assert!(
            result.stdout.contains("robber-test-ok") || result.stderr.contains("robber-test-ok"),
            "stdout={} stderr={}",
            result.stdout,
            result.stderr
        );
        assert_eq!(result.memory_cap_bytes, DEFAULT_MEMORY_CAP);
        fs::remove_dir_all(&dir).ok();
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
}
