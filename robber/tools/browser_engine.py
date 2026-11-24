"""Isolated lightweight headless browser automation engine for UI testing and visual debugging."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from robber.config import (
    BROWSER_MEMORY_BOUND_MB,
    BROWSER_SNAPSHOT_DIR,
    BROWSER_TIMEOUT_SECONDS,
    BROWSER_VIEWPORT,
)
from robber.jail import workspace_root

logger = logging.getLogger(__name__)

# Low-spec resource-throttling flags (capped under 300 MB RAM)
CHROMIUM_LOW_MEM_FLAGS = [
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-gpu",
    "--single-process",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-breakpad",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-domain-reliability",
    "--disable-sync",
    "--disable-translate",
    "--metrics-recording-only",
    "--mute-audio",
    "--no-first-run",
    "--safebrowsing-disable-auto-update",
    '--js-flags="--max-old-space-size=256"',
]


@dataclass
class BrowserSnapshot:
    url: str
    title: str = ""
    screenshot_path: str = ""
    dom_html: str = ""
    accessibility_tree: list[dict[str, Any]] | dict[str, Any] = field(default_factory=list)
    computed_styles: dict[str, Any] = field(default_factory=dict)
    console_errors: list[dict[str, Any]] = field(default_factory=list)
    page_errors: list[dict[str, Any]] = field(default_factory=list)
    failed_requests: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = True
    status_code: int = 200
    elapsed_ms: int = 0
    engine: str = "playwright"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class PlaywrightBrowserEngine:
    """Low-footprint async browser controller with memory-bound isolation."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        snapshot_dir: Path | None = None,
        memory_bound_mb: int = BROWSER_MEMORY_BOUND_MB,
    ) -> None:
        self.root = (root or workspace_root()).resolve()
        self.snapshot_dir = snapshot_dir or (self.root / BROWSER_SNAPSHOT_DIR)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.memory_bound_mb = memory_bound_mb
        self._playwright = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()

    async def _ensure_browser(self):
        if self._browser is not None and self._browser.is_connected():
            return self._browser

        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        launch_args = list(CHROMIUM_LOW_MEM_FLAGS)
        launch_args.append(f'--js-flags="--max-old-space-size={self.memory_bound_mb}"')

        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=launch_args,
        )
        return self._browser

    async def navigate_and_capture(
        self,
        url: str,
        viewport: tuple[int, int] = BROWSER_VIEWPORT,
        timeout: float = BROWSER_TIMEOUT_SECONDS,
        wait_until: str = "load",
        capture_screenshot: bool = True,
    ) -> BrowserSnapshot:
        """Render target web application page and capture DOM, telemetry, and screenshots."""
        t0 = time.monotonic()
        target_url = url if "://" in url else f"http://{url}"

        console_errors: list[dict[str, Any]] = []
        page_errors: list[dict[str, Any]] = []
        failed_requests: list[dict[str, Any]] = []

        try:
            async with self._lock:
                browser = await self._ensure_browser()
                context = await browser.new_context(
                    viewport={"width": viewport[0], "height": viewport[1]},
                    device_scale_factor=1.0,
                    ignore_https_errors=True,
                )

                page = await context.new_page()

                # Attach listeners
                page.on(
                    "console",
                    lambda msg: console_errors.append(
                        {"type": msg.type, "text": msg.text, "location": msg.location}
                    )
                    if msg.type in ("error", "warning")
                    else None,
                )
                page.on(
                    "pageerror",
                    lambda err: page_errors.append(
                        {"message": str(err), "type": type(err).__name__}
                    ),
                )
                page.on(
                    "requestfailed",
                    lambda req: failed_requests.append(
                        {
                            "url": req.url,
                            "method": req.method,
                            "failure": req.failure.error_text if req.failure else "failed",
                        }
                    ),
                )

                response = await page.goto(
                    target_url,
                    timeout=int(timeout * 1000),
                    wait_until=wait_until,
                )

                status_code = response.status if response else 200
                title = await page.title()
                dom_html = await page.content()

                # Accessibility Tree
                try:
                    ax_tree = await page.accessibility.snapshot()
                except Exception:
                    ax_tree = {}

                # Screenshot
                screenshot_rel = ""
                if capture_screenshot:
                    snap_id = f"snap_{uuid.uuid4().hex[:10]}.png"
                    snap_path = self.snapshot_dir / snap_id
                    await page.screenshot(path=str(snap_path), full_page=False, type="png")
                    try:
                        screenshot_rel = str(snap_path.relative_to(self.root)).replace("\\", "/")
                    except ValueError:
                        screenshot_rel = str(snap_path)

                # Basic computed styles sample
                try:
                    computed = await page.evaluate(
                        """() => {
                            const body = document.body;
                            if (!body) return {};
                            const style = window.getComputedStyle(body);
                            return {
                                backgroundColor: style.backgroundColor,
                                color: style.color,
                                fontFamily: style.fontFamily,
                                width: document.documentElement.clientWidth,
                                height: document.documentElement.clientHeight
                            };
                        }"""
                    )
                except Exception:
                    computed = {}

                await page.close()
                await context.close()

                elapsed_ms = int((time.monotonic() - t0) * 1000)
                ok = status_code < 400 and not page_errors

                return BrowserSnapshot(
                    url=target_url,
                    title=title,
                    screenshot_path=screenshot_rel,
                    dom_html=dom_html,
                    accessibility_tree=ax_tree or {},
                    computed_styles=computed or {},
                    console_errors=console_errors,
                    page_errors=page_errors,
                    failed_requests=failed_requests,
                    ok=ok,
                    status_code=status_code,
                    elapsed_ms=elapsed_ms,
                    engine="playwright-chromium",
                )

        except Exception as exc:
            logger.info("Playwright browser capture fell back (%s)", exc)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return BrowserSnapshot(
                url=target_url,
                title="Browser Capture Failed",
                screenshot_path="",
                dom_html=f"<!-- Error navigating to {target_url}: {exc} -->",
                accessibility_tree={},
                computed_styles={},
                console_errors=[{"type": "error", "text": str(exc), "location": {}}],
                page_errors=[{"message": str(exc), "type": type(exc).__name__}],
                failed_requests=failed_requests,
                ok=False,
                status_code=500,
                elapsed_ms=elapsed_ms,
                engine="fallback",
            )

    async def close(self) -> None:
        """Cleanly close all browser processes and Playwright instances."""
        async with self._lock:
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

    async def __aenter__(self) -> PlaywrightBrowserEngine:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
