"""Framed JSON client for the Go gateway IPC socket.

Length prefix is 4-byte big-endian. Payloads must not include API keys.
Default listen address matches gateway/bridge: 127.0.0.1:8791 on Windows.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
from collections.abc import Callable
from typing import Any

from robber.config import GATEWAY_IPC_HOST, GATEWAY_IPC_PORT

HEADER = struct.Struct("!I")
MAX_FRAME = 1 << 20
Handler = Callable[[dict[str, Any]], dict[str, Any] | None]


def default_addr() -> str:
    env = (os.environ.get("GATEWAY_IPC") or "").strip()
    if env:
        return env
    return f"{GATEWAY_IPC_HOST}:{int(GATEWAY_IPC_PORT)}"


def split_addr(addr: str) -> tuple[str, str]:
    raw = (addr or "").strip()
    if raw.startswith("unix:"):
        return "unix", raw[5:]
    if raw.startswith("tcp:"):
        return "tcp", raw[4:]
    if "/" in raw or raw.endswith(".sock"):
        return "unix", raw
    return "tcp", raw or default_addr()


def encode_frame(message: dict[str, Any]) -> bytes:
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ValueError("ipc frame exceeds 1 MiB")
    return HEADER.pack(len(body)) + body


def decode_frame(blob: bytes) -> dict[str, Any]:
    if len(blob) < 4:
        raise ValueError("ipc frame header truncated")
    size = HEADER.unpack(blob[:4])[0]
    if size > MAX_FRAME:
        raise ValueError("ipc frame exceeds 1 MiB")
    payload = blob[4 : 4 + size]
    if len(payload) != size:
        raise ValueError("ipc frame body truncated")
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("ipc payload must be an object")
    return data


def _recvall(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        piece = sock.recv(size - len(chunks))
        if not piece:
            raise ConnectionError("ipc socket closed")
        chunks.extend(piece)
    return bytes(chunks)


def read_frame(sock: socket.socket) -> dict[str, Any]:
    size = HEADER.unpack(_recvall(sock, 4))[0]
    if size == 0 or size > MAX_FRAME:
        raise ValueError("invalid ipc frame length")
    data = json.loads(_recvall(sock, size).decode("utf-8"))
    if not isinstance(data, dict) or not str(data.get("type") or "").strip():
        raise ValueError("ipc type is empty")
    return data


def write_frame(sock: socket.socket, message: dict[str, Any]) -> None:
    sock.sendall(encode_frame(message))


class GatewayClient:
    """Dial the Go IPC server and optionally serve inbound task frames."""

    def __init__(self, addr: str | None = None, timeout: float = 2.0) -> None:
        self.addr = addr or default_addr()
        self.timeout = float(timeout)
        self.sock: socket.socket | None = None
        self._lock = threading.Lock()

    def connect(self) -> "GatewayClient":
        network, address = split_addr(self.addr)
        if network == "unix":
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(address)
        else:
            host, port_s = address.rsplit(":", 1)
            sock = socket.create_connection((host, int(port_s)), timeout=self.timeout)
            sock.settimeout(self.timeout)
        self.sock = sock
        return self

    def close(self) -> None:
        sock = self.sock
        self.sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def send(self, kind: str, payload: Any | None = None, msg_id: str = "") -> None:
        body: dict[str, Any] = {"type": kind, "id": msg_id, "payload": payload or {}}
        with self._lock:
            write_frame(self._require(), body)

    def recv(self) -> dict[str, Any]:
        with self._lock:
            return read_frame(self._require())

    def ping(self) -> dict[str, Any]:
        self.send("ping", {"ok": True}, msg_id="ping")
        return self.recv()

    def notify_ssot(self, table: str, row_id: str = "") -> None:
        self.send("ssot_mutated", {"table": table, "row_id": row_id})

    def serve(self, handler: Handler, stop: threading.Event | None = None) -> None:
        flag = stop or threading.Event()
        sock = self._require()
        while not flag.is_set():
            try:
                sock.settimeout(0.25)
                msg = read_frame(sock)
            except TimeoutError:
                continue
            except socket.timeout:
                continue
            except OSError:
                return
            reply = handler(msg)
            if reply:
                if "id" not in reply:
                    reply["id"] = msg.get("id") or ""
                with self._lock:
                    write_frame(sock, reply)

    def _require(self) -> socket.socket:
        if self.sock is None:
            raise RuntimeError("gateway ipc is not connected")
        return self.sock


_ipc_global: GatewayClient | None = None


def _get_ipc() -> GatewayClient | None:
    global _ipc_global
    if _ipc_global is not None and _ipc_global.sock is not None:
        return _ipc_global
    try:
        client = GatewayClient()
        client.connect()
        _ipc_global = client
        return client
    except Exception:
        return None


def broadcast_question_ipc(question: Any) -> None:
    """Send a clarification question to the Go gateway IPC for TUI display."""
    client = _get_ipc()
    if client is None:
        return
    try:
        payload = question.to_dict() if hasattr(question, "to_dict") else dict(question)
        client.send("question_broadcast", payload)
    except Exception:
        pass
