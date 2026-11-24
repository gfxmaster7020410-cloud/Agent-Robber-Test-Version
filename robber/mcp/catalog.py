"""Local MCP marketplace catalog — persisted under .robber, no secrets."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from robber.jail import workspace_root

CATALOG_NAME = "mcp.json"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
ALLOWED_BINS = {
    "python",
    "python.exe",
    "py",
    "py.exe",
    "node",
    "node.exe",
}
ALLOWED_MODULES = {"robber.mcp.echo_server"}

BUILTIN_SEED: list[dict[str, Any]] = [
    {
        "id": "jailed-fs",
        "label": "Host filesystem",
        "kind": "builtin",
        "description": "list_dir, read_file, and search_files anywhere on the local machine.",
        "enabled": True,
        "builtin": True,
        "command": "",
        "args": [],
        "env_keys": [],
    },
    {
        "id": "ssot-memory",
        "label": "SSOT memory",
        "kind": "builtin",
        "description": "Search the local hash-json workspace index.",
        "enabled": True,
        "builtin": True,
        "command": "",
        "args": [],
        "env_keys": [],
    },
    {
        "id": "git-readonly",
        "label": "Git (read-only)",
        "kind": "builtin",
        "description": "status, diff, and log — no push, reset, or commit.",
        "enabled": True,
        "builtin": True,
        "command": "",
        "args": [],
        "env_keys": [],
    },
    {
        "id": "echo-stdio",
        "label": "Echo (stdio probe)",
        "kind": "stdio",
        "description": "Local JSON-RPC echo server used to verify the MCP stdio transport.",
        "enabled": False,
        "builtin": True,
        "command": "python",
        "args": ["-m", "robber.mcp.echo_server"],
        "env_keys": [],
    },
]


def catalog_path() -> Path:
    return workspace_root() / ".robber" / CATALOG_NAME


def _read() -> dict[str, Any]:
    path = catalog_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(payload: dict[str, Any]) -> None:
    path = catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _normalize(row: dict[str, Any]) -> dict[str, Any] | None:
    server_id = str(row.get("id") or "").strip().lower()
    if not ID_RE.match(server_id):
        return None
    kind = str(row.get("kind") or "stdio").strip().lower()
    if kind not in {"builtin", "stdio"}:
        kind = "stdio"
    builtin = bool(row.get("builtin")) or any(seed["id"] == server_id for seed in BUILTIN_SEED)
    if builtin:
        kind = str(next((seed["kind"] for seed in BUILTIN_SEED if seed["id"] == server_id), kind))
    args = row.get("args") if isinstance(row.get("args"), list) else []
    env_keys = row.get("env_keys") if isinstance(row.get("env_keys"), list) else []
    return {
        "id": server_id,
        "label": str(row.get("label") or server_id).strip() or server_id,
        "kind": kind,
        "description": str(row.get("description") or "").strip(),
        "enabled": bool(row.get("enabled")),
        "builtin": builtin,
        "command": str(row.get("command") or "").strip(),
        "args": [str(item) for item in args][:16],
        "env_keys": [str(item).strip() for item in env_keys if str(item).strip()][:16],
    }


def load_catalog() -> list[dict[str, Any]]:
    stored = _read()
    rows = stored.get("servers") if isinstance(stored.get("servers"), list) else []
    by_id: dict[str, dict[str, Any]] = {}
    for seed in BUILTIN_SEED:
        by_id[seed["id"]] = dict(seed)
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized = _normalize(row)
        if normalized is None:
            continue
        if normalized["id"] in by_id and by_id[normalized["id"]]["builtin"]:
            by_id[normalized["id"]]["enabled"] = normalized["enabled"]
            continue
        by_id[normalized["id"]] = normalized
    servers = list(by_id.values())
    _write({"servers": servers})
    return servers


def save_catalog(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned = [row for row in (_normalize(item) for item in servers) if row is not None]
    _write({"servers": cleaned})
    return cleaned


def get_server(server_id: str) -> dict[str, Any] | None:
    needle = server_id.strip().lower()
    for row in load_catalog():
        if row["id"] == needle:
            return row
    return None


def set_enabled(server_id: str, enabled: bool) -> dict[str, Any]:
    needle = server_id.strip().lower()
    servers = load_catalog()
    for row in servers:
        if row["id"] == needle:
            row["enabled"] = bool(enabled)
            save_catalog(servers)
            return row
    raise ValueError(f"unknown MCP server '{server_id}'")


def add_server(
    *,
    server_id: str,
    label: str,
    command: str,
    args: list[str] | None = None,
    env_keys: list[str] | None = None,
    description: str = "",
    enabled: bool = False,
) -> dict[str, Any]:
    needle = server_id.strip().lower()
    if not ID_RE.match(needle):
        raise ValueError("id must be lowercase letters, digits, dot, underscore, or hyphen")
    if any(seed["id"] == needle for seed in BUILTIN_SEED):
        raise ValueError("cannot overwrite a built-in MCP server")
    row = _normalize(
        {
            "id": needle,
            "label": label,
            "kind": "stdio",
            "description": description,
            "enabled": enabled,
            "builtin": False,
            "command": command,
            "args": args or [],
            "env_keys": env_keys or [],
        }
    )
    if row is None:
        raise ValueError("invalid MCP server row")
    validate_stdio(row)
    servers = [item for item in load_catalog() if item["id"] != needle]
    servers.append(row)
    save_catalog(servers)
    return row


def remove_server(server_id: str) -> dict[str, Any]:
    needle = server_id.strip().lower()
    servers = load_catalog()
    found = next((row for row in servers if row["id"] == needle), None)
    if found is None:
        raise ValueError(f"unknown MCP server '{server_id}'")
    if found["builtin"]:
        raise ValueError("built-in MCP servers cannot be removed — disable them instead")
    save_catalog([row for row in servers if row["id"] != needle])
    return found


def validate_stdio(row: dict[str, Any]) -> None:
    if row["kind"] != "stdio":
        return
    binary = Path(str(row.get("command") or "")).name.lower()
    if binary not in ALLOWED_BINS:
        raise ValueError(f"MCP command not allowlisted: {row.get('command')}")
    args = [str(item) for item in row.get("args") or []]
    if any(".." in item.replace("\\", "/") for item in args):
        raise ValueError("MCP args cannot contain ..")
    if args[:2] == ["-m", "robber.mcp.echo_server"]:
        return
    if not args:
        raise ValueError("stdio MCP servers need a jailed script argument")
    script = args[0]
    if script.startswith("-"):
        raise ValueError("stdio MCP args cannot start with a flag except python -m echo_server")
    from robber.jail import JailError, resolve_inside

    try:
        target = resolve_inside(workspace_root(), script)
    except JailError as exc:
        raise ValueError(str(exc)) from exc
    if not target.is_file():
        raise ValueError(f"MCP script is not a file inside the jail: {script}")
    if target.suffix.lower() not in {".py", ".js", ".mjs", ".cjs"}:
        raise ValueError("MCP script must be .py or .js inside the workspace")
