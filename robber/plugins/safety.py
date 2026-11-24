"""Static safety checks for third-party plugin files."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from robber.config import PLUGIN_MAX_BYTES
from robber.plugins.base_plugin import PluginError

PLUGIN_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_FORBIDDEN_IMPORTS = {"pickle", "marshal", "dill"}
_FORBIDDEN_CALLS = {"eval", "exec", "compile"}


def confined_plugin_path(root: Path, path: Path) -> Path:
    plugins = (root / ".robber" / "plugins").resolve()
    target = path.resolve()
    try:
        target.relative_to(plugins)
    except ValueError as exc:
        raise PluginError("plugin path escapes .robber/plugins") from exc
    return target


def file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def validate_plugin_file(root: Path, path: Path) -> str:
    target = confined_plugin_path(root, path)
    if not target.is_file():
        raise PluginError("plugin file is missing")
    suffix = target.suffix.lower()
    if suffix not in {".py", ".so", ".dll"}:
        raise PluginError(f"unsupported plugin type {suffix}")
    stem = target.stem
    if not PLUGIN_NAME_RE.match(stem):
        raise PluginError("plugin filename is not a safe identifier")
    size = target.stat().st_size
    if size <= 0 or size > PLUGIN_MAX_BYTES:
        raise PluginError("plugin file size is outside the allowed range")
    digest = file_digest(target)
    expected = declared_digest(root, stem, target)
    if expected and expected != digest:
        raise PluginError("plugin safety signature mismatch")
    if suffix == ".py":
        scan_python_source(target)
    return digest


def declared_digest(root: Path, name: str, path: Path) -> str:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if sidecar.is_file():
        return sidecar.read_text(encoding="ascii", errors="replace").strip().split()[0].lower()
    manifest = (root / ".robber" / "plugins" / "manifest.json").resolve()
    if not manifest.is_file():
        return ""
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PluginError("plugin manifest.json is not valid json") from exc
    allow = data.get("allow") if isinstance(data, dict) else None
    if not isinstance(allow, dict):
        return ""
    row = allow.get(name)
    if isinstance(row, str):
        return row.strip().lower()
    if isinstance(row, dict):
        return str(row.get("sha256") or "").strip().lower()
    return ""


def scan_python_source(path: Path) -> None:
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source, filename=path.name)
    except SyntaxError as exc:
        raise PluginError(f"plugin syntax error: {exc.msg}") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _ban_import(alias.name)
        elif isinstance(node, ast.ImportFrom):
            _ban_import(node.module or "")
        elif isinstance(node, ast.Call):
            _ban_call(node)


def _ban_import(name: str) -> None:
    root = (name or "").split(".", 1)[0].strip().lower()
    if root in _FORBIDDEN_IMPORTS:
        raise PluginError(f"plugin import '{root}' is not allowed")


def _ban_call(node: ast.Call) -> None:
    func = node.func
    ident = ""
    if isinstance(func, ast.Name):
        ident = func.id
    elif isinstance(func, ast.Attribute):
        ident = func.attr
        if isinstance(func.value, ast.Name) and func.value.id == "os" and func.attr == "system":
            raise PluginError("os.system is not allowed in plugins")
        if isinstance(func.value, ast.Name) and func.value.id == "os" and func.attr == "popen":
            raise PluginError("os.popen is not allowed in plugins")
    if ident in _FORBIDDEN_CALLS:
        raise PluginError(f"call '{ident}' is not allowed in plugins")
    for kw in node.keywords:
        if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            raise PluginError("shell=True is not allowed in plugins")


def validate_args(schema_parameters: dict[str, Any], raw: dict[str, Any] | None) -> dict[str, Any]:
    incoming = dict(raw or {})
    extra = set(incoming) - set(schema_parameters)
    for key in extra:
        incoming.pop(key, None)
    out: dict[str, Any] = {}
    for name, spec in schema_parameters.items():
        required = bool(spec.get("required")) if isinstance(spec, dict) else bool(getattr(spec, "required", False))
        default = spec.get("default") if isinstance(spec, dict) else getattr(spec, "default", None)
        kind = spec.get("type") if isinstance(spec, dict) else getattr(spec, "type", "string")
        if name not in incoming:
            if required:
                raise PluginError(f"missing required arg '{name}'")
            if default is not None:
                out[name] = default
            continue
        out[name] = _coerce(incoming[name], str(kind or "string"), name)
    return out


def _coerce(value: Any, kind: str, name: str) -> Any:
    try:
        if kind == "integer":
            return int(value)
        if kind == "number":
            return float(value)
        if kind == "boolean":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in {"1", "true", "yes"}:
                return True
            if text in {"0", "false", "no"}:
                return False
            raise ValueError("not boolean")
        if kind in {"object", "array"}:
            if kind == "object" and not isinstance(value, dict):
                raise ValueError("not object")
            if kind == "array" and not isinstance(value, list):
                raise ValueError("not array")
            return value
        return str(value)
    except (TypeError, ValueError) as exc:
        raise PluginError(f"arg '{name}' has the wrong type") from exc
