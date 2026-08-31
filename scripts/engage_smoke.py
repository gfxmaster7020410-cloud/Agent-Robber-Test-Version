"""Live daemon engage smoke — never prints secrets."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

import websockets

BASE = "http://127.0.0.1:8787"
WS = "ws://127.0.0.1:8787/ws"


def get_json(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def redact_state(frame: dict) -> dict:
    keys = frame.get("keys")
    if isinstance(keys, dict):
        frame = {**frame, "keys": {name: bool(ok) for name, ok in keys.items()}}
    loop = frame.get("loop") if isinstance(frame.get("loop"), dict) else {}
    return {
        "type": frame.get("type"),
        "mode": frame.get("mode"),
        "control_mode": frame.get("control_mode"),
        "model": frame.get("model"),
        "engine": frame.get("engine"),
        "live": frame.get("live"),
        "busy": frame.get("busy"),
        "keys": frame.get("keys"),
        "loop_phase": loop.get("phase"),
        "healer": loop.get("healer_bound"),
        "vision": loop.get("vision_ready"),
    }


async def run() -> int:
    print("=== REST / ===")
    root = get_json("/")
    print(
        json.dumps(
            {
                "name": root.get("name"),
                "ws": root.get("ws"),
                "engine": root.get("engine"),
                "control_mode": root.get("control_mode"),
                "self_heal": root.get("self_heal"),
                "vision": root.get("vision"),
                "loop": root.get("loop"),
                "keys": {name: bool(ok) for name, ok in (root.get("keys") or {}).items()},
            },
            indent=2,
        )
    )

    print("\n=== REST /api/system/health ===")
    health = get_json("/api/system/health")
    engines = health.get("engines") or {}
    print(
        json.dumps(
            {
                "ok": health.get("ok"),
                "control_mode": health.get("control_mode"),
                "phase": health.get("phase"),
                "engines": {
                    name: {
                        "ok": item.get("ok"),
                        "detail": item.get("detail"),
                        "count": item.get("count"),
                    }
                    for name, item in engines.items()
                    if isinstance(item, dict)
                },
            },
            indent=2,
        )
    )

    print("\n=== WS engage ===")
    async with websockets.connect(WS, open_timeout=8) as ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
        print(
            "hello",
            json.dumps(redact_state(first) if first.get("type") == "state" else {"type": first.get("type")}),
        )
        await ws.send(json.dumps({"type": "ping"}))
        pinged = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
        print(
            "ping",
            json.dumps(redact_state(pinged) if pinged.get("type") == "state" else {"type": pinged.get("type")}),
        )
        await ws.send(json.dumps({"type": "set_mode", "mode": "autonomous"}))
        await ws.send(json.dumps({"type": "keys_status"}))
        await ws.send(json.dumps({"type": "prompt", "text": "Reply with exactly one word: pong"}))
        saw_assistant = False
        saw_loop = False
        saw_keys = False
        deadline = asyncio.get_event_loop().time() + 45
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
            except TimeoutError:
                break
            frame = json.loads(raw)
            kind = frame.get("type")
            role = frame.get("role")
            if kind == "loop_state":
                saw_loop = True
                print("loop_state", (frame.get("content") or "")[:80])
            elif kind == "message" and role == "system":
                content = str(frame.get("content") or "")
                if "BYOK" in content or "ready" in content.lower():
                    saw_keys = True
                print("system", content[:160].replace("\n", " | "))
            elif kind == "message" and role == "assistant":
                saw_assistant = True
                print("assistant", str(frame.get("content") or "")[:200].replace("\n", " "))
            elif kind == "state":
                print("state", json.dumps(redact_state(frame)))
            elif kind in {"self_heal", "busy", "error"}:
                print(kind, str(frame.get("content") or frame.get("label") or "")[:120])
            if saw_assistant and kind == "state" and frame.get("busy") is False:
                break
        print(
            json.dumps(
                {
                    "saw_loop": saw_loop,
                    "saw_keys_report": saw_keys,
                    "saw_assistant": saw_assistant,
                }
            )
        )
        return 0 if saw_assistant else 2


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run()))
    except urllib.error.URLError as exc:
        print("daemon unreachable:", type(exc).__name__)
        raise SystemExit(1)
