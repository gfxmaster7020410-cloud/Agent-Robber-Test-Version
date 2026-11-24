"""Async WebSocket bridge to the Agent Robber daemon workbench channel."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

DEFAULT_URL = "ws://127.0.0.1:8787/ws/dashboard"
RECONNECT_STEPS = (0.5, 1.0, 2.0, 4.0, 5.0)

OnFrame = Callable[[dict[str, Any]], Awaitable[None] | None]
OnStatus = Callable[[str], Awaitable[None] | None]


class DaemonClient:
    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        on_frame: OnFrame | None = None,
        on_status: OnStatus | None = None,
    ) -> None:
        self.url = url
        self.on_frame = on_frame
        self.on_status = on_status
        self._ws: Any = None
        self._stop = asyncio.Event()
        self._open = False

    @property
    def online(self) -> bool:
        return self._open and self._ws is not None

    async def _emit_status(self, status: str) -> None:
        if self.on_status is None:
            return
        maybe = self.on_status(status)
        if asyncio.iscoroutine(maybe):
            await maybe

    async def _emit_frame(self, frame: dict[str, Any]) -> None:
        if self.on_frame is None:
            return
        maybe = self.on_frame(frame)
        if asyncio.iscoroutine(maybe):
            await maybe

    def send(self, payload: dict[str, Any]) -> bool:
        ws = self._ws
        if ws is None or not self._open:
            return False
        data = json.dumps(payload)
        try:
            asyncio.get_running_loop().create_task(ws.send(data))
            return True
        except Exception:
            return False

    async def send_async(self, payload: dict[str, Any]) -> bool:
        ws = self._ws
        if ws is None or not self._open:
            return False
        try:
            await ws.send(json.dumps(payload))
            return True
        except Exception:
            return False

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            await self._emit_status("connecting" if attempt == 0 else "offline")
            try:
                async with websockets.connect(
                    self.url,
                    open_timeout=4,
                    close_timeout=1,
                    max_size=None,
                ) as ws:
                    self._ws = ws
                    self._open = True
                    attempt = 0
                    await self._emit_status("online")
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        if not isinstance(raw, str):
                            continue
                        try:
                            frame = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(frame, dict):
                            await self._emit_frame(frame)
            except Exception:
                pass
            finally:
                self._open = False
                self._ws = None
                await self._emit_status("offline")
            if self._stop.is_set():
                break
            wait = RECONNECT_STEPS[min(attempt, len(RECONNECT_STEPS) - 1)]
            attempt += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                continue
