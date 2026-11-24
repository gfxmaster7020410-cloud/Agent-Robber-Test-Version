#!/usr/bin/env python3

"""Install and configure local LSP binaries for Agent Robber.



Keeps Node heaps at 512 MB so language servers fit the 1.5 GB combined RSS cap

on 8GB RAM / i5 hardware.

"""



from __future__ import annotations



import json

import os

import shutil

import subprocess

import sys

from pathlib import Path



ROOT_DIR = Path(__file__).resolve().parents[1]

CONFIG_DIR = ROOT_DIR / "robber" / "config"

LSP_CONFIG_PATH = CONFIG_DIR / "lsp_servers.json"

NODE_MEM_LIMIT_ENV = {"NODE_OPTIONS": "--max-old-space-size=512"}

GO_FALLBACK = r"D:\Program Files\Go\bin\go.exe"





def _spawn_argv(cmd: list[str]) -> list[str]:

    if os.name != "nt" or not cmd:

        return cmd

    first = cmd[0]

    if first.lower().endswith((".cmd", ".bat")):

        return ["cmd.exe", "/c", *cmd]

    return cmd





def run_cmd(cmd: list[str], env_extra: dict | None = None, allow_fail: bool = False, timeout: int = 180) -> bool:

    env = os.environ.copy()

    if env_extra:

        env.update(env_extra)

    resolved = list(cmd)

    if resolved:

        found = shutil.which(resolved[0]) if not os.path.isfile(resolved[0]) else resolved[0]

        if found:

            resolved[0] = found

    print(f"  [RUNNING] {' '.join(resolved)}")

    try:

        subprocess.run(

            _spawn_argv(resolved),

            env=env,

            stdout=subprocess.PIPE,

            stderr=subprocess.PIPE,

            text=True,

            check=True,

            timeout=timeout,

        )

        print("  [OK] Success")

        return True

    except FileNotFoundError as exc:

        if not allow_fail:

            print(f"  [FAIL] Command failed: {exc}")

        return False

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:

        if not allow_fail:

            print(f"  [FAIL] Command failed: {exc}")

            err = getattr(exc, "stderr", "") or ""

            if err:

                print(f"  [ERROR OUTPUT] {str(err).strip()[:800]}")

        return False





def check_binary(name: str) -> str | None:

    found = shutil.which(name)

    return found





def _first_on_path(*names: str) -> str | None:

    for name in names:

        found = check_binary(name)

        if found:

            return found

    return None





def install_python_lsp() -> str | None:

    print("\n--- [1/3] Configuring Python LSP (Pyright) ---")

    existing = _first_on_path("pyright-langserver", "pyright-python-langserver", "pyright")

    if existing:

        print("  [EXISTS] Python language server already in PATH.")

        return existing

    if run_cmd([sys.executable, "-m", "pip", "install", "pyright"], allow_fail=True):

        found = _first_on_path("pyright-langserver", "pyright-python-langserver", "pyright")

        if found:

            return found

    npm_bin = check_binary("npm")

    if npm_bin and run_cmd(

        [npm_bin, "install", "-g", "pyright"],

        env_extra=NODE_MEM_LIMIT_ENV,

        allow_fail=True,

    ):

        found = _first_on_path("pyright-langserver", "pyright")

        if found:

            return found

    print("  [WARNING] Failed to install Pyright. Python type checking stays in-process AST.")

    return None





def install_typescript_lsp() -> str | None:

    print("\n--- [2/3] Configuring TypeScript/JavaScript LSP ---")

    existing = check_binary("typescript-language-server")

    if existing:

        print("  [EXISTS] typescript-language-server already in PATH.")

        return existing

    npm_bin = check_binary("npm")

    if npm_bin:

        if run_cmd(

            [npm_bin, "install", "-g", "typescript-language-server", "typescript"],

            env_extra=NODE_MEM_LIMIT_ENV,

            allow_fail=True,

        ):

            found = check_binary("typescript-language-server")

            if found:

                return found

    print("  [WARNING] npm not found or install failed. TS/JS LSP disabled.")

    return None





def install_go_lsp() -> str | None:

    print("\n--- [3/3] Configuring Go LSP (gopls) ---")

    existing = check_binary("gopls")

    if existing:

        print("  [EXISTS] gopls already in PATH.")

        return existing

    go_bin = check_binary("go") or (GO_FALLBACK if os.path.isfile(GO_FALLBACK) else None)

    if not go_bin:

        print("  [WARNING] Go toolchain missing. gopls disabled.")

        return None

    run_cmd([go_bin, "install", "golang.org/x/tools/gopls@latest"], allow_fail=True)

    found = _find_gopls(go_bin)

    if found:

        return found

    print("  [WARNING] gopls install failed. Go LSP disabled.")

    return None





def _go_env(go_bin: str, key: str) -> str:

    try:

        res = subprocess.run(

            [go_bin, "env", key],

            stdout=subprocess.PIPE,

            stderr=subprocess.DEVNULL,

            text=True,

            timeout=10,

            check=True,

        )

        return (res.stdout or "").strip()

    except (subprocess.SubprocessError, OSError):

        return ""





def _find_gopls(go_bin: str) -> str | None:

    named = "gopls.exe" if os.name == "nt" else "gopls"

    existing = check_binary("gopls")

    if existing:

        return existing

    folders = [

        _go_env(go_bin, "GOBIN"),

        os.environ.get("GOBIN") or "",

        str(Path(_go_env(go_bin, "GOPATH") or os.environ.get("GOPATH") or Path.home() / "go") / "bin"),

    ]

    for folder in folders:

        if not folder:

            continue

        candidate = Path(folder) / named

        if candidate.is_file():

            return str(candidate)

    return None





def generate_lsp_config(

    pyright_path: str | None,

    ts_path: str | None,

    gopls_path: str | None,

    dest: Path | None = None,

) -> Path:

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    path = dest or LSP_CONFIG_PATH

    py_cmd = _stdio_cmd(pyright_path)

    ts_cmd = _stdio_cmd(ts_path)

    go_cmd = [gopls_path] if gopls_path else []

    lsp_config = {

        "version": "1.0",

        "memory_bound_mb": 512,

        "servers": {

            "python": {

                "enabled": bool(py_cmd),

                "command": py_cmd,

                "languages": ["python"],

                "root_markers": ["pyproject.toml", "setup.py", "requirements.txt", ".git"],

            },

            "typescript": {

                "enabled": bool(ts_cmd),

                "command": ts_cmd,

                "languages": ["typescript", "javascript", "typescriptreact", "javascriptreact"],

                "root_markers": ["package.json", "tsconfig.json", ".git"],

                "env": {"NODE_OPTIONS": "--max-old-space-size=512"},

            },

            "go": {

                "enabled": bool(go_cmd),

                "command": go_cmd,

                "languages": ["go"],

                "root_markers": ["go.mod", "go.sum", ".git"],

            },

        },

    }

    path.write_text(json.dumps(lsp_config, indent=2), encoding="utf-8")

    print(f"\n[SUCCESS] LSP configuration written to {path}")

    return path





def _stdio_cmd(binary: str | None) -> list[str]:

    if not binary:

        return []

    path = Path(binary)

    if path.suffix.lower() == ".cmd":

        cli = path.parent / "node_modules" / path.stem / "lib" / "cli.mjs"

        node = check_binary("node")

        if node and cli.is_file():

            return [node, str(cli), "--stdio"]

    if "gopls" in path.name.lower():

        return [binary]

    return [binary, "--stdio"]





def main() -> int:

    print("==========================================================")

    print(" Agent Robber - LSP Auto-Installer and Setup ")

    print("==========================================================")

    py_path = install_python_lsp()

    ts_path = install_typescript_lsp()

    go_path = install_go_lsp()

    generate_lsp_config(py_path, ts_path, go_path)

    return 0





if __name__ == "__main__":

    raise SystemExit(main())

