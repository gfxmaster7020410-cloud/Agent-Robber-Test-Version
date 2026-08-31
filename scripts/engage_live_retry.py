"""Retry live completion on batch=normal. Never prints secrets."""

from __future__ import annotations

import asyncio
import json

import websockets

WS = "ws://127.0.0.1:8787/ws"


async def drain(ws, seconds: float = 1.5) -> None:
    deadline = asyncio.get_event_loop().time() + seconds
    while asyncio.get_event_loop().time() < deadline:
        remaining = deadline - asyncio.get_event_loop().time()
        try:
            await asyncio.wait_for(ws.recv(), timeout=max(0.05, remaining))
        except TimeoutError:
            return


async def main() -> int:
    async with websockets.connect(WS, open_timeout=8) as ws:
        await drain(ws, 1.0)
        await ws.send(json.dumps({"type": "set_batch", "batch": "normal"}))
        await ws.send(json.dumps({"type": "set_consensus", "models": ["Gemini Ultra"]}))
        await ws.send(json.dumps({"type": "set_model", "model": "Gemini Ultra"}))
        await drain(ws, 1.5)
        await ws.send(json.dumps({"type": "ping_model", "label": "Gemini Ultra"}))
        await drain(ws, 6.0)
        await ws.send(json.dumps({"type": "prompt", "text": "Reply with exactly one word: pong"}))
        saw_live = False
        saw_fallback = False
        failover = []
        assistant = ""
        engine = ""
        deadline = asyncio.get_event_loop().time() + 50
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
            except TimeoutError:
                break
            frame = json.loads(raw)
            kind = frame.get("type")
            role = frame.get("role")
            content = str(frame.get("content") or "")
            if kind == "state":
                engine = str(frame.get("engine") or engine)
                if frame.get("busy") is False and (saw_live or saw_fallback or assistant):
                    break
            elif kind == "message" and role == "system":
                if "failing over" in content or "unhealthy" in content or "health check" in content:
                    failover.append(content[:160])
                print("system", content[:180].replace("\n", " | "))
            elif kind == "message" and role == "assistant":
                assistant += content
                if "agent plan preview" in content:
                    saw_fallback = True
                else:
                    saw_live = True
                print("assistant", content[:180].replace("\n", " "))
            elif kind == "error":
                print("error", content[:180])
            elif kind == "busy":
                print("busy", frame.get("label"))
        print(
            json.dumps(
                {
                    "engine": engine,
                    "saw_live_tokens": saw_live,
                    "saw_canned_fallback": saw_fallback,
                    "failovers": failover,
                    "assistant_preview": assistant[:120],
                }
            )
        )
        return 0 if (saw_live or saw_fallback) else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
