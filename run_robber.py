"""Agent Robber — Startup Script (Module 01).

Initialises the database, starts the backend API server in a background
thread, waits for it to become healthy, then launches the TUI.

Usage:
    python run_robber.py              # default: server + TUI
    python run_robber.py --server     # server only
    python run_robber.py --tui        # TUI only (assumes server already running)
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("run-robber")

API_HOST = "127.0.0.1"
API_PORT = 8765
API_BASE = f"http://{API_HOST}:{API_PORT}"
HEALTH_TIMEOUT = 15  # seconds to wait for server


# ---------------------------------------------------------------------------
# Server launcher (background thread)
# ---------------------------------------------------------------------------

def _start_server_thread() -> threading.Thread:
    """Run the FastAPI server in a daemon thread."""
    from agent_robber.backend.api_server import run_server

    thread = threading.Thread(target=run_server, args=(API_HOST, API_PORT), daemon=True)
    thread.start()
    return thread


def _wait_for_server() -> bool:
    """Block until the /health endpoint responds or timeout."""
    deadline = time.monotonic() + HEALTH_TIMEOUT
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{API_BASE}/health", timeout=2.0)
            if resp.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------------------
# TUI launcher
# ---------------------------------------------------------------------------

def _start_tui() -> None:
    from agent_robber.tui.app import run_tui
    run_tui(api_base=API_BASE)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Robber v1.0.0 — Startup")
    parser.add_argument("--server", action="store_true", help="Run server only (no TUI)")
    parser.add_argument("--tui", action="store_true", help="Run TUI only (server must be running)")
    args = parser.parse_args()

    run_server = args.server or (not args.tui)
    run_tui_flag = args.tui or (not args.server)

    log.info("═══ AGENT ROBBER v1.0.0 ═══")

    if run_server:
        log.info("Starting backend server on %s:%d ...", API_HOST, API_PORT)
        _start_server_thread()
        if not _wait_for_server():
            log.error("Server failed to become healthy within %ds", HEALTH_TIMEOUT)
            sys.exit(1)
        log.info("Backend server is healthy ✓")

    if run_tui_flag:
        log.info("Launching TUI ...")
        _start_tui()
    else:
        log.info("Server running. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            log.info("Shutting down.")


if __name__ == "__main__":
    main()
