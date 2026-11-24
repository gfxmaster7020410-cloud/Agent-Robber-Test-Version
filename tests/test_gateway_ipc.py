from __future__ import annotations

import json
import socket
import threading

from robber.gateway_ipc import GatewayClient, decode_frame, encode_frame, split_addr


def test_split_addr_tcp_and_unix() -> None:
    assert split_addr("127.0.0.1:8791") == ("tcp", "127.0.0.1:8791")
    assert split_addr("tcp:127.0.0.1:8791") == ("tcp", "127.0.0.1:8791")
    assert split_addr("unix:/tmp/gateway.sock") == ("unix", "/tmp/gateway.sock")


def test_frame_roundtrip_socketpair() -> None:
    left, right = socket.socketpair()
    try:
        raw = encode_frame({"type": "task", "id": "abc", "payload": {"prompt": "hi"}})
        left.sendall(raw)
        got = decode_frame(_recvall(right, len(raw)))
        assert got["type"] == "task"
        assert got["id"] == "abc"
        assert got["payload"]["prompt"] == "hi"
    finally:
        left.close()
        right.close()


def test_gateway_client_ping_loopback() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()
    ready = threading.Event()

    def serve() -> None:
        conn, _ = srv.accept()
        try:
            ready.set()
            blob = _recvall(conn, 4)
            import struct

            n = struct.unpack("!I", blob)[0]
            msg = json.loads(_recvall(conn, n).decode("utf-8"))
            assert msg["type"] == "ping"
            conn.sendall(encode_frame({"type": "pong", "id": msg.get("id") or "", "payload": {"ok": True}}))
        finally:
            conn.close()
            srv.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = GatewayClient(f"{host}:{port}", timeout=2.0)
    client.connect()
    try:
        reply = client.ping()
        assert reply["type"] == "pong"
        assert reply["payload"]["ok"] is True
    finally:
        client.close()
        thread.join(timeout=2)


def _recvall(sock: socket.socket, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        assert chunk
        buf.extend(chunk)
    return bytes(buf)
