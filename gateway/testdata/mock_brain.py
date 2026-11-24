"""Mock Python brain for gateway IPC tests. No API keys, loopback only."""

from __future__ import annotations

import json
import socket
import sqlite3
import struct
import sys
import time

HEADER = struct.Struct("!I")
MAX_FRAME = 1 << 20


def recvall(sock: socket.socket, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf.extend(chunk)
    return bytes(buf)


def read_frame(sock: socket.socket) -> dict:
    n = HEADER.unpack(recvall(sock, 4))[0]
    if n == 0 or n > MAX_FRAME:
        raise ValueError("bad frame")
    return json.loads(recvall(sock, n).decode("utf-8"))


def write_frame(sock: socket.socket, obj: dict) -> None:
    body = json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    sock.sendall(HEADER.pack(len(body)) + body)


def connect(addr: str) -> socket.socket:
    if addr.startswith("unix:"):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(8)
        sock.connect(addr[5:])
        return sock
    host, port = addr.rsplit(":", 1)
    sock = socket.create_connection((host, int(port)), timeout=8)
    sock.settimeout(8)
    return sock


def bump_task(db_path: str, task_id: str) -> None:
    if not db_path or not task_id:
        return
    con = sqlite3.connect(db_path, timeout=5)
    try:
        con.execute(
            "UPDATE task_state SET status=?, updated_at=? WHERE id=?",
            ("running", time.time(), task_id),
        )
        con.commit()
    finally:
        con.close()


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: mock_brain.py HOST:PORT [ssot.db]")
    sock = connect(sys.argv[1])
    db_path = sys.argv[2] if len(sys.argv) > 2 else ""
    write_frame(sock, {"type": "ping", "id": "hello", "payload": {"ok": True}})
    while True:
        msg = read_frame(sock)
        kind = str(msg.get("type") or "")
        if kind == "pong":
            continue
        if kind != "task":
            continue
        payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
        task_id = str(payload.get("task_id") or "")
        route = payload.get("route") if isinstance(payload.get("route"), dict) else {}
        primary = route.get("primary") if isinstance(route.get("primary"), dict) else {}
        label = str(primary.get("label") or "")
        bump_task(db_path, task_id)
        write_frame(
            sock,
            {
                "type": "task_ack",
                "id": str(msg.get("id") or ""),
                "payload": {"ok": True, "primary": label, "task_id": task_id},
            },
        )
        write_frame(
            sock,
            {
                "type": "ssot_mutated",
                "id": str(msg.get("id") or ""),
                "payload": {"table": "task_state", "row_id": task_id},
            },
        )


if __name__ == "__main__":
    main()
