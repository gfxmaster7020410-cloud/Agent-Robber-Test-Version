#!/usr/bin/env python3
"""Single-command polyglot release bundler for Agent Robber.

Builds (or copies) the Go gateway, Rust indexer cdylib, TypeScript TUI, and
Python runtime into dist/agent-robber. Never copies .env, key files, or
accounts.db.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robber.launcher import find_cargo, find_go, rust_build_env, rust_cdylib
from robber.ssot_db import SCHEMA_VERSION

SKIP_FILE_NAMES = {
    ".env",
    ".env.local",
    "keys.vault",
    "credentials.json",
    "accounts.db",
    "account.key",
    "master.key",
}
SKIP_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "target",
    ".tools",
    ".robber",
}


def package(
    dest: Path | None = None,
    *,
    skip_native: bool = False,
    skip_ui: bool = False,
) -> dict[str, Any]:
    out = Path(dest).resolve() if dest else ROOT / "dist" / "agent-robber"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    artifacts: dict[str, str] = {}

    _copy_python(out)
    artifacts["python"] = "robber/"
    _copy_if(ROOT / "server.py", out / "server.py")
    _copy_if(ROOT / "tui.py", out / "tui.py")
    _copy_if(ROOT / "main.py", out / "main.py")
    _copy_if(ROOT / "requirements.txt", out / "requirements.txt")
    _copy_if(ROOT / ".env.example", out / ".env.example")
    _copy_if(ROOT / "robber.bat", out / "robber.bat")
    if (ROOT / "bin").is_dir():
        shutil.copytree(ROOT / "bin", out / "bin", dirs_exist_ok=True)
        artifacts["bin"] = "bin/"
    _verify_python(out, notes)

    if not skip_native:
        gw = _build_gateway(out, notes)
        if gw:
            artifacts["gateway"] = str(gw.relative_to(out)).replace("\\", "/")
        dll = _build_rust(out, notes)
        if dll:
            artifacts["indexer"] = str(dll.relative_to(out)).replace("\\", "/")
    else:
        notes.append("native compile skipped")
        _copy_prebuilt(out, artifacts)

    if not skip_ui:
        ui = _build_ui(out, notes)
        if ui:
            artifacts["ui"] = str(ui.relative_to(out)).replace("\\", "/")
    else:
        notes.append("ui transpile skipped")

    _write_launchers(out)
    artifacts["launch"] = "launch.cmd" if sys.platform == "win32" else "launch.sh"
    readme = out / "README-RELEASE.txt"
    readme.write_text(_readme(), encoding="utf-8")
    artifacts["readme"] = readme.name
    manifest = {
        "name": "agent-robber",
        "schema_version": SCHEMA_VERSION,
        "created_at": time.time(),
        "platform": sys.platform,
        "artifacts": artifacts,
        "notes": notes,
        "rss_cap_bytes": int(1.5 * 1024 * 1024 * 1024),
        "local_context_window": 4096,
    }
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    artifacts["manifest"] = "MANIFEST.json"
    return {"dest": str(out), **manifest}


def _copy_python(out: Path) -> None:
    src = ROOT / "robber"
    dest = out / "robber"

    def ignore(directory: str, names: list[str]) -> list[str]:
        skipped: list[str] = []
        for name in names:
            path = Path(directory) / name
            if name in SKIP_DIR_NAMES and path.is_dir():
                skipped.append(name)
            elif name in SKIP_FILE_NAMES or name.endswith(".pyc"):
                skipped.append(name)
        return skipped

    shutil.copytree(src, dest, ignore=ignore)


def _copy_if(src: Path, dest: Path) -> None:
    if src.is_file() and src.name not in SKIP_FILE_NAMES:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def _build_gateway(out: Path, notes: list[str]) -> Path | None:
    go = find_go()
    name = "gateway.exe" if sys.platform == "win32" else "gateway"
    dest = out / "gateway" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not go:
        notes.append("go toolchain missing")
        pre = ROOT / "gateway" / name
        if pre.is_file():
            shutil.copy2(pre, dest)
            return dest
        return None
    env = os.environ.copy()
    env["CGO_ENABLED"] = "0"
    code = _run(
        [go, "build", "-ldflags=-s -w", "-o", str(dest), "."],
        cwd=ROOT / "gateway",
        env=env,
    )
    if code != 0:
        notes.append("go build failed")
        return None
    return dest if dest.is_file() else None


def _build_rust(out: Path, notes: list[str]) -> Path | None:
    cargo = find_cargo()
    dest_dir = out / "indexer"
    dest_dir.mkdir(parents=True, exist_ok=True)
    if cargo:
        env = rust_build_env(ROOT)
        code = _run(
            [
                cargo,
                "build",
                "--release",
                "--manifest-path",
                str(ROOT / "indexer" / "Cargo.toml"),
            ],
            cwd=ROOT,
            env=env,
        )
        if code != 0:
            notes.append("cargo --release failed")
    dll = rust_cdylib(ROOT)
    if dll is None or not dll.is_file():
        notes.append("rust cdylib missing")
        copied = _copy_rust_bins(dest_dir)
        if copied:
            notes.append("rust bins copied without cdylib")
        return None
    target = dest_dir / dll.name
    shutil.copy2(dll, target)
    _copy_rust_bins(dest_dir)
    return target


def _copy_rust_bins(dest_dir: Path) -> list[str]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for src in _iter_rust_bins(ROOT):
        shutil.copy2(src, dest_dir / src.name)
        copied.append(src.name)
    return copied


def _iter_rust_bins(root: Path) -> list[Path]:
    names = ("robber-indexer.exe", "robber-indexer", "tools.exe", "tools")
    extra = (os.environ.get("CARGO_TARGET_DIR") or "").strip()
    bases = [root / "indexer" / "target"]
    if extra:
        bases.append(Path(extra))
    triples = (
        "",
        "x86_64-pc-windows-gnu",
        "x86_64-pc-windows-msvc",
        "x86_64-unknown-linux-gnu",
        "aarch64-apple-darwin",
        "x86_64-apple-darwin",
    )
    found: list[Path] = []
    seen: set[str] = set()
    for base in bases:
        for triple in triples:
            stem = base / triple if triple else base
            folder = stem / "release"
            for name in names:
                path = folder / name
                if path.is_file() and name not in seen:
                    seen.add(name)
                    found.append(path)
    return found


def _copy_prebuilt(out: Path, artifacts: dict[str, str]) -> None:
    name = "gateway.exe" if sys.platform == "win32" else "gateway"
    gw = ROOT / "gateway" / name
    if gw.is_file():
        dest = out / "gateway" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(gw, dest)
        artifacts["gateway"] = str(dest.relative_to(out)).replace("\\", "/")
    dll = rust_cdylib(ROOT)
    dest_dir = out / "indexer"
    dest_dir.mkdir(parents=True, exist_ok=True)
    if dll is not None and dll.is_file():
        dest = dest_dir / dll.name
        shutil.copy2(dll, dest)
        artifacts["indexer"] = str(dest.relative_to(out)).replace("\\", "/")
    bins = _copy_rust_bins(dest_dir)
    if bins:
        artifacts["indexer_bins"] = ",".join(bins)


def _build_ui(out: Path, notes: list[str]) -> Path | None:
    src = ROOT / "ui" / "src" / "index.tsx"
    if not src.is_file():
        notes.append("ui/src/index.tsx missing")
        return None
    dest_dir = out / "ui"
    dest_dir.mkdir(parents=True, exist_ok=True)
    outfile = dest_dir / "robber-ui.mjs"
    npx = shutil.which("npx")
    if npx:
        code = _run(
            [
                npx,
                "--yes",
                "esbuild",
                str(src),
                "--bundle",
                "--minify",
                "--platform=node",
                "--format=esm",
                "--packages=external",
                f"--outfile={outfile}",
            ],
            cwd=ROOT / "ui",
        )
        if code == 0 and outfile.is_file():
            shutil.copy2(ROOT / "ui" / "package.json", dest_dir / "package.json")
            return outfile
        notes.append("esbuild ui skipped or failed")
    dest_src = dest_dir / "src"
    if dest_src.exists():
        shutil.rmtree(dest_src)
    shutil.copytree(ROOT / "ui" / "src", dest_src)
    shutil.copy2(ROOT / "ui" / "package.json", dest_dir / "package.json")
    shutil.copy2(ROOT / "ui" / "tsconfig.json", dest_dir / "tsconfig.json")
    notes.append("ui sources copied (run npm install && npm start in ui/)")
    return dest_src


def _verify_python(out: Path, notes: list[str]) -> None:
    req = out / "requirements.txt"
    if not req.is_file():
        notes.append("requirements.txt missing")
    elif "cryptography" not in req.read_text(encoding="utf-8").lower():
        notes.append("requirements.txt missing cryptography")
    if not (out / "robber" / "launcher.py").is_file():
        notes.append("launcher entry point missing")
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="robber-ssot-verify-"))
    try:
        from robber.ssot_db import SCHEMA_VERSION as schema, SsotDB

        db = SsotDB(tmp / "ssot.db")
        db.connect()
        snap = db.snapshot()
        if str(snap.get("version")) != str(schema):
            notes.append("ssot migration version mismatch")
        tables = snap.get("tables") or []
        if "credential_store" not in tables:
            notes.append("credential_store missing after migrate")
        db.close()
    except Exception as exc:
        notes.append(f"ssot migration check failed: {type(exc).__name__}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _write_launchers(out: Path) -> None:
    sh = """#!/bin/sh
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd)${PYTHONPATH:+:$PYTHONPATH}"
exec python -m robber.launcher --skip-build "$@"
"""
    (out / "launch.sh").write_text(sh.replace("\r\n", "\n"), encoding="utf-8")
    cmd = (
        "@echo off\r\n"
        "setlocal\r\n"
        "cd /d \"%~dp0\"\r\n"
        "set PYTHONPATH=%CD%;%PYTHONPATH%\r\n"
        "python -m robber.launcher --skip-build %*\r\n"
    )
    (out / "launch.cmd").write_text(cmd, encoding="utf-8")
    try:
        os.chmod(out / "launch.sh", 0o755)
    except OSError:
        pass


def _readme() -> str:
    return (
        "Agent Robber release bundle\n"
        "===========================\n"
        "\n"
        "1. Install Python 3.12+ and `pip install -r requirements.txt`.\n"
        "2. Optional: install Ollama and pull qwen2.5-coder:3b (8GB RAM).\n"
        "3. Launch with launch.cmd (Windows) or ./launch.sh.\n"
        "4. SSOT migrates itself to schema 4 on first connect (WAL).\n"
        "5. API keys are stored AES-256-GCM in .robber/ssot.db; never commit that file.\n"
        "6. Combined RSS cap is 1.5 GB. Local model context is 4096 tokens.\n"
        "\n"
        "This folder must not contain .env or accounts.db.\n"
    )


def _run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> int:
    import subprocess

    try:
        proc = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True)
    except OSError:
        return 1
    return int(proc.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Package Agent Robber for local release")
    parser.add_argument("--dest", default="", help="output directory")
    parser.add_argument("--skip-native", action="store_true", help="do not compile Go/Rust")
    parser.add_argument("--skip-ui", action="store_true", help="do not transpile the TS TUI")
    args = parser.parse_args(argv)
    dest = Path(args.dest).resolve() if args.dest else None
    report = package(dest, skip_native=bool(args.skip_native), skip_ui=bool(args.skip_ui))
    sys.stdout.write(json.dumps({"ok": True, "dest": report["dest"], "artifacts": report["artifacts"]}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
