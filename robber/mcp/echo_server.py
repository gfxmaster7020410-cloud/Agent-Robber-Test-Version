"""Local JSON-RPC echo server used to probe the MCP stdio transport."""

from __future__ import annotations

import json
import sys
from typing import Any


def _read_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        decoded = line.decode("ascii", errors="replace")
        key, _, value = decoded.partition(":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length") or "0")
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length)
    if not body:
        return None
    payload = json.loads(body.decode("utf-8"))
    return payload if isinstance(payload, dict) else None


def _write_message(payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    sys.stdout.buffer.write(header + body)
    sys.stdout.buffer.flush()


def _ok(msg_id: object, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: object, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": message}}


def main() -> None:
    while True:
        message = _read_message()
        if message is None:
            return
        method = str(message.get("method") or "")
        msg_id = message.get("id")
        if method == "initialize":
            _write_message(
                _ok(
                    msg_id,
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "robber-echo", "version": "1.0.0"},
                    },
                )
            )
            continue
        if method == "notifications/initialized":
            continue
        if method == "tools/list":
            _write_message(
                _ok(
                    msg_id,
                    {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "Echo a text argument back (stdio probe).",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                },
                            }
                        ]
                    },
                )
            )
            continue
        if method == "tools/call":
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            name = str(params.get("name") or "")
            args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            if name != "echo":
                _write_message(_err(msg_id, f"unknown tool {name}"))
                continue
            text = str(args.get("text") or args.get("message") or "")
            _write_message(
                _ok(
                    msg_id,
                    {
                        "content": [{"type": "text", "text": text}],
                        "isError": False,
                    },
                )
            )
            continue
        if method == "ping":
            _write_message(_ok(msg_id, {}))
            continue
        if msg_id is not None:
            _write_message(_err(msg_id, f"unknown method {method}"))


if __name__ == "__main__":
    main()
