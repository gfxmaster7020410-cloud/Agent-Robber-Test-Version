"""Phase 8 Tauri shell — config and safety contract, no Rust toolchain required."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAURI = ROOT / "gui" / "src-tauri"


def _fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def _ok(message: str) -> None:
    print(f"ok: {message}")


def main() -> None:
    conf_path = TAURI / "tauri.conf.json"
    cargo_path = TAURI / "Cargo.toml"
    lib_path = TAURI / "src" / "lib.rs"
    main_path = TAURI / "src" / "main.rs"
    caps_path = TAURI / "capabilities" / "default.json"
    if not conf_path.is_file():
        _fail("missing gui/src-tauri/tauri.conf.json")

    conf = json.loads(conf_path.read_text(encoding="utf-8"))
    if conf.get("identifier") != "com.agentrobber.controlcenter":
        _fail("unexpected bundle identifier")
    if conf.get("build", {}).get("devUrl") != "http://127.0.0.1:5173":
        _fail("devUrl must stay on 127.0.0.1:5173")
    if conf.get("build", {}).get("frontendDist") != "../dist":
        _fail("frontendDist must be the existing Vite dist")
    csp = str(conf.get("app", {}).get("security", {}).get("csp") or "")
    if "127.0.0.1:8787" not in csp:
        _fail("CSP must allow the local daemon")
    if "https:" in csp.split("connect-src")[-1].split(";")[0] and "*" in csp:
        _fail("CSP connect-src is too open")
    if "https://*" in csp or "wss://*" in csp:
        _fail("CSP must not allow arbitrary remote sockets")
    _ok("tauri.conf localhost + CSP")

    cargo = cargo_path.read_text(encoding="utf-8")
    if "tauri-plugin-shell" in cargo or "tauri-plugin-fs" in cargo:
        _fail("desktop crate must not expose shell/fs plugins to the webview")
    if 'name = "agent-robber-desktop"' not in cargo:
        _fail("Cargo package name mismatch")
    _ok("no shell/fs plugins")

    caps = json.loads(caps_path.read_text(encoding="utf-8"))
    perms = caps.get("permissions") or []
    if perms != ["core:default"]:
        _fail(f"capabilities too wide: {perms}")
    _ok("minimal capabilities")

    lib = lib_path.read_text(encoding="utf-8")
    main_rs = main_path.read_text(encoding="utf-8")
    banned = ("litellm", "openai", "gemini", "playwright", "mcp_call", "execute_tool")
    blob = lib.lower() + main_rs.lower()
    for word in banned:
        if word in blob:
            _fail(f"desktop crate contains agent logic marker: {word}")
    if "--host" not in lib or "127.0.0.1" not in lib:
        _fail("daemon spawn must bind 127.0.0.1")
    if "uvicorn" not in lib or "server:app" not in lib:
        _fail("daemon spawn must launch uvicorn server:app")
    if re.search(r"\breload\b", lib):
        _fail("desktop uvicorn must not enable reload")
    if "spawned_here" not in lib:
        _fail("must track whether this process started the daemon")
    _ok("spawn contract")

    for name in ("32x32.png", "128x128.png", "icon.png", "icon.ico"):
        path = TAURI / "icons" / name
        if not path.is_file() or path.stat().st_size < 32:
            _fail(f"missing icon {name}")
    _ok("icons")

    vite = (ROOT / "gui" / "vite.config.js").read_text(encoding="utf-8")
    if "src-tauri" not in vite:
        _fail("vite must ignore src-tauri watches")
    _ok("vite desktop config")


if __name__ == "__main__":
    main()
    print("tauri phase 8 tests passed")
