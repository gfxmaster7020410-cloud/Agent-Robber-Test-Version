#!/usr/bin/env python3

"""Minimal LSP stdio stub used by tests/test_lsp_client.py."""



from __future__ import annotations



import json

import sys





def read_msg() -> dict:

    headers: dict[str, str] = {}

    while True:

        line = sys.stdin.buffer.readline()

        if not line:

            raise EOFError

        if line in (b"\r\n", b"\n"):

            break

        decoded = line.decode("utf-8", errors="replace")

        if ":" in decoded:

            key, value = decoded.split(":", 1)

            headers[key.strip().lower()] = value.strip()

    length = int(headers.get("content-length") or "0")

    body = sys.stdin.buffer.read(length)

    return json.loads(body.decode("utf-8"))





def send_msg(payload: dict) -> None:

    raw = json.dumps(payload, ensure_ascii=True).encode("utf-8")

    sys.stdout.buffer.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw)

    sys.stdout.buffer.flush()





def main() -> int:

    opened: str | None = None

    while True:

        try:

            msg = read_msg()

        except (EOFError, json.JSONDecodeError):

            return 0

        method = str(msg.get("method") or "")

        mid = msg.get("id")

        if method == "initialize":

            send_msg({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {"textDocumentSync": 1}}})

            continue

        if method == "initialized":

            continue

        if method == "textDocument/didOpen":

            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

            doc = params.get("textDocument") if isinstance(params.get("textDocument"), dict) else {}

            opened = str(doc.get("uri") or "")

            text = str(doc.get("text") or "")

            items = []

            if "broken" in text:

                items = [

                    {

                        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},

                        "severity": 1,

                        "message": "stub error",

                    }

                ]

            send_msg(

                {

                    "jsonrpc": "2.0",

                    "method": "textDocument/publishDiagnostics",

                    "params": {"uri": opened, "diagnostics": items},

                }

            )

            continue

        if method == "textDocument/didClose":

            opened = None

            continue

        if method == "shutdown":

            send_msg({"jsonrpc": "2.0", "id": mid, "result": None})

            continue

        if method == "exit":

            return 0

        if mid is not None:

            send_msg({"jsonrpc": "2.0", "id": mid, "result": None})

    return 0





if __name__ == "__main__":

    raise SystemExit(main())

