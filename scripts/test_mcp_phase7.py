"""Phase 7 MCP host tests — catalog, builtins, jail, stdio echo."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", "test-placeholder"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def _ok(message: str) -> None:
    print(f"ok: {message}")


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="robber-mcp-") as raw:
        work = Path(raw)
        (work / "hello.txt").write_text("phase7 mcp jail probe\n", encoding="utf-8")
        os.environ["ROBBER_WORKSPACE"] = str(work)

        from robber.mcp.catalog import add_server, load_catalog, set_enabled, validate_stdio
        from robber.mcp.host import McpHost, parse_mcp_request
        from robber.terminal import TerminalSupervisor

        servers = load_catalog()
        ids = {row["id"] for row in servers}
        if not {"jailed-fs", "ssot-memory", "git-readonly", "echo-stdio"} <= ids:
            _fail(f"builtin catalog missing entries: {sorted(ids)}")
        catalog_file = work / ".robber" / "mcp.json"
        if not catalog_file.is_file():
            _fail("catalog did not persist to .robber/mcp.json")
        stored = json.loads(catalog_file.read_text(encoding="utf-8"))
        if "servers" not in stored:
            _fail("catalog json missing servers")
        _ok("catalog persist")

        try:
            add_server(
                server_id="escape",
                label="escape",
                command="python",
                args=["../outside.py"],
            )
            _fail("jail allowed a path that escapes the workspace")
        except ValueError:
            _ok("jail blocks escaped script")

        try:
            validate_stdio(
                {
                    "kind": "stdio",
                    "command": "cmd.exe",
                    "args": ["hello.py"],
                }
            )
            _fail("cmd.exe was allowlisted")
        except ValueError:
            _ok("stdio bin allowlist")

        host = McpHost()
        supervisor = TerminalSupervisor(root=work)
        await host.start(supervisor)
        listed = host.format_catalog()
        if "jailed-fs" not in listed:
            _fail("builtin list missing jailed-fs")
        result = await host.call("jailed-fs", "list_dir", {"path": "."})
        if not result.ok or "hello.txt" not in result.text:
            _fail(f"list_dir failed: {result.text}")
        _ok("builtin list_dir")

        read = await host.call("jailed-fs", "read_file", {"path": "hello.txt"})
        if not read.ok or "phase7 mcp jail probe" not in read.text:
            _fail(f"read_file failed: {read.text}")
        _ok("builtin read_file")

        escaped = await host.call("jailed-fs", "read_file", {"path": "../hello.txt"})
        if escaped.ok is False and "not a file" not in (escaped.text or "").lower() and "escapes" in (escaped.text or "").lower():
            _fail("read_file still jail-blocks parent paths")
        _ok("builtin read outside workspace root")

        request = parse_mcp_request('mcp call jailed-fs list_dir {"path":"."}')
        if request is None or request.server != "jailed-fs" or request.tool != "list_dir":
            _fail("parse_mcp_request failed for slash call")
        _ok("parse call")

        echo_row = set_enabled("echo-stdio", True)
        if echo_row["id"] != "echo-stdio":
            _fail("enable echo-stdio failed")
        await host.enable("echo-stdio")
        echo = await host.call("echo-stdio", "echo", {"text": "pong-7"})
        if not echo.ok or "pong-7" not in echo.text:
            _fail(f"stdio echo failed: {echo.text}")
        _ok("stdio handshake + echo")

        snap = host.snapshot()
        if not any(tool["qualified"] == "mcp__echo-stdio__echo" for tool in snap["tools"]):
            _fail("qualified echo tool missing from snapshot")
        _ok("snapshot tools")

        await host.stop()


if __name__ == "__main__":
    asyncio.run(main())
    print("mcp phase 7 tests passed")
