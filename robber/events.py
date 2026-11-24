"""WebSocket sync envelopes for the workbench dashboard.

Existing clients keep reading `type` / `role` / `content`. The desktop UI also
reads `event` + `payload` (LOG_STREAM, STATE_SYNC, FILE_MUTATION_UPDATE) so both
contracts travel on the same frame.
"""

from __future__ import annotations

from typing import Any

_LAST_COMMAND = ""
_ELAPSED_MS = 0
_FRAME_SEQ = 0


def next_frame_id() -> str:
    global _FRAME_SEQ
    _FRAME_SEQ += 1
    return f"f{_FRAME_SEQ}"


def stamp_frame(frame: dict[str, Any]) -> dict[str, Any]:
    if isinstance(frame, dict) and not frame.get("id"):
        frame["id"] = next_frame_id()
    return frame


def set_last_command(text: str) -> None:
    global _LAST_COMMAND
    _LAST_COMMAND = (text or "").strip()


def last_command() -> str:
    return _LAST_COMMAND


def set_elapsed_ms(value: int) -> None:
    global _ELAPSED_MS
    _ELAPSED_MS = max(0, int(value))


def elapsed_ms() -> int:
    return _ELAPSED_MS


def stream_kind_for_role(role: str) -> str:
    lowered = (role or "system").strip().lower()
    if lowered in {"user", "system", "memory", "reasoning"}:
        return "initiating"
    if lowered == "error":
        return "error"
    if lowered in {"file", "assistant", "success"}:
        return "writing"
    if lowered == "tool":
        return "terminal"
    return "initiating"


def state_ack_frame(status: str = "routing", content: str = "Routing to a live provider") -> dict[str, Any]:
    text = (content or "Routing to a live provider").strip() or "Routing to a live provider"
    return stamp_frame(
        {
            "type": "message",
            "event": "STATE_ACK",
            "role": "system",
            "content": text,
            "text": text,
            "stream_kind": "ack",
            "status": status,
            "append": False,
        }
    )


def log_stream_frame(
    *,
    kind: str,
    content: str,
    command: str | None = None,
    timestamp_ms: int | None = None,
) -> dict[str, Any]:
    payload = {
        "type": kind,
        "command": command if command is not None else last_command(),
        "content": content,
        "timestamp_ms": elapsed_ms() if timestamp_ms is None else max(0, int(timestamp_ms)),
    }
    role = "error" if kind == "error" else "user" if kind == "initiating" else "file" if kind == "writing" else "tool"
    return {
        "event": "LOG_STREAM",
        "type": "message",
        "role": role,
        "content": content,
        "payload": payload,
        "append": False,
    }


def mutation_sync_frame(payload: dict[str, Any]) -> dict[str, Any]:
    return stamp_frame(
        {
            "event": "FILE_MUTATION_UPDATE",
            "type": "mutations",
            "payload": payload,
            "mutations": payload,
        }
    )


def attach_sync_envelope(frame: dict[str, Any]) -> dict[str, Any]:
    """Stamp `event` / `payload` onto an existing daemon frame without dropping fields."""
    if not isinstance(frame, dict):
        return frame
    kind = str(frame.get("type") or "")
    event = str(frame.get("event") or "")
    stream_kind = str(frame.get("stream_kind") or "")
    if kind == "state" and not event:
        frame["event"] = "STATE_SYNC"
    elif kind in {"state_ack", "ack"} or event == "STATE_ACK" or stream_kind == "ack":
        frame["event"] = "STATE_ACK"
        frame.setdefault("stream_kind", "ack")
        content = str(frame.get("content") or frame.get("text") or "Routing to a live provider")
        frame.setdefault("content", content)
        frame.setdefault("text", content)
        frame.setdefault(
            "payload",
            {
                "type": "ack",
                "command": last_command(),
                "content": content,
                "timestamp_ms": elapsed_ms(),
            },
        )
    elif kind == "mutations" and not event:
        frame["event"] = "FILE_MUTATION_UPDATE"
        frame.setdefault("payload", frame.get("mutations") or {})
    elif kind in {"message", "error", "log"} and event not in {"LOG_STREAM", "STATE_ACK"}:
        role = str(frame.get("role") or ("error" if kind == "error" else "system"))
        stream_kind = stream_kind_for_role(role if kind != "error" else "error")
        if kind == "error":
            stream_kind = "error"
        content = str(frame.get("content") or "")
        frame["event"] = "LOG_STREAM"
        payload = frame.setdefault(
            "payload",
            {
                "type": stream_kind,
                "command": last_command(),
                "content": content,
                "timestamp_ms": elapsed_ms(),
            },
        )
        if isinstance(payload, dict):
            if frame.get("ansi") is not None:
                payload.setdefault("ansi", frame.get("ansi"))
            if frame.get("stream_kind"):
                payload.setdefault("type", frame.get("stream_kind"))
    elif kind == "busy" and not event:
        frame["event"] = "BUSY"
    elif kind == "mutations" and event == "FILE_MUTATION_UPDATE":
        frame.setdefault("payload", frame.get("mutations") or {})
    return stamp_frame(frame)
