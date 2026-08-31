//! Desktop host for the Control Center.
//!
//! This crate must not contain agent logic. It only:
//! - opens the existing Vite/React GUI
//! - starts `uvicorn` on 127.0.0.1:8787 when that port is free
//! - stops the child it started when the window closes

use std::fs::{self, OpenOptions};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{AppHandle, Manager, RunEvent};

const DAEMON_HOST: &str = "127.0.0.1";
const DAEMON_PORT: u16 = 8787;

struct DaemonProcess {
    child: Mutex<Option<Child>>,
    spawned_here: bool,
}

pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let state = match boot_daemon() {
                Ok(state) => state,
                Err(err) => {
                    eprintln!("desktop daemon boot skipped: {err}");
                    DaemonProcess {
                        child: Mutex::new(None),
                        spawned_here: false,
                    }
                }
            };
            app.manage(state);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Agent Robber desktop")
        .run(|app, event| {
            if matches!(event, RunEvent::Exit | RunEvent::ExitRequested { .. }) {
                stop_daemon(app);
            }
        });
}

fn boot_daemon() -> Result<DaemonProcess, String> {
    if daemon_listening() {
        return Ok(DaemonProcess {
            child: Mutex::new(None),
            spawned_here: false,
        });
    }
    let workspace = workspace_root()?;
    if !workspace.join("server.py").is_file() {
        return Err(format!("server.py not found in {}", workspace.display()));
    }
    let log_dir = workspace.join(".robber");
    fs::create_dir_all(&log_dir).map_err(|err| err.to_string())?;
    let log_path = log_dir.join("desktop-daemon.log");
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|err| err.to_string())?;
    let err_log = log.try_clone().map_err(|err| err.to_string())?;

    let mut command = python_command();
    command
        .current_dir(&workspace)
        .args([
            "-m",
            "uvicorn",
            "server:app",
            "--host",
            DAEMON_HOST,
            "--port",
            &DAEMON_PORT.to_string(),
            "--log-level",
            "info",
        ])
        .env("ROBBER_WORKSPACE", &workspace)
        .stdin(Stdio::null())
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(err_log));

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    let child = command
        .spawn()
        .map_err(|err| format!("failed to spawn uvicorn: {err}"))?;
    Ok(DaemonProcess {
        child: Mutex::new(Some(child)),
        spawned_here: true,
    })
}

fn stop_daemon(app: &AppHandle) {
    if let Some(state) = app.try_state::<DaemonProcess>() {
        if !state.spawned_here {
            return;
        }
        if let Ok(mut guard) = state.child.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

fn daemon_listening() -> bool {
    let addr = format!("{DAEMON_HOST}:{DAEMON_PORT}");
    TcpStream::connect_timeout(&addr.parse().unwrap(), Duration::from_millis(350)).is_ok()
}

fn python_command() -> Command {
    if let Ok(explicit) = std::env::var("PYTHON") {
        if !explicit.trim().is_empty() {
            return Command::new(explicit);
        }
    }
    Command::new("python")
}

fn workspace_root() -> Result<PathBuf, String> {
    if let Ok(raw) = std::env::var("ROBBER_WORKSPACE") {
        let path = PathBuf::from(raw);
        if path.join("server.py").is_file() {
            return Ok(path);
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        let start = exe.parent().unwrap_or_else(|| exe.as_path());
        if let Some(found) = walk_for_server(start) {
            return Ok(found);
        }
    }
    let compiled = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    compiled
        .parent()
        .and_then(Path::parent)
        .map(Path::to_path_buf)
        .filter(|path| path.join("server.py").is_file())
        .ok_or_else(|| "could not locate ROBBER_WORKSPACE / server.py".to_string())
}

fn walk_for_server(start: &Path) -> Option<PathBuf> {
    let mut current = Some(start);
    while let Some(dir) = current {
        if dir.join("server.py").is_file() {
            return Some(dir.to_path_buf());
        }
        current = dir.parent();
    }
    None
}
