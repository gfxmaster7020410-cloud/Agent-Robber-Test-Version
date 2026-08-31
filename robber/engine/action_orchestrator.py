import asyncio
import logging
import random
import time
from typing import Any, Optional

from robber.engine.browser_stealth import StealthBrowserEngine
from robber.engine.proxy_rotator import ProxyManager
from robber.engine.vault import SnapshotVaultManager

logger = logging.getLogger(__name__)

DISMISS_SELECTORS = (
    'button[aria-label="Close"]',
    'button[aria-label="close"]',
    '[aria-label="Dismiss"]',
    'button:has-text("Not now")',
    'button:has-text("Accept cookies")',
)


class ActionOrchestrator:
    def __init__(
        self,
        browser: StealthBrowserEngine,
        profile_id: str,
        hub,
        session_manager,
        vault: SnapshotVaultManager,
        proxy_manager: ProxyManager,
        vision_inspector=None,
        healer=None,
    ):
        self.browser = browser
        self.profile_id = profile_id
        self.hub = hub
        self.session_manager = session_manager
        self.vault = vault
        self.proxy_manager = proxy_manager
        self.vision = vision_inspector
        self.healer = healer
        self.metrics = {"actions_completed": 0, "failures": 0}

    async def _handle_error(self, e: Exception, page: Optional[Any] = None, error_type: str = "EXECUTION_ERROR"):
        self.metrics["failures"] += 1
        await self._broadcast_action(f"Error encountered: {error_type} - {type(e).__name__}")

        report = None
        if page:
            try:
                image_path = await self.vault.capture_failure_snapshot(
                    page, self.profile_id, error_type, type(e).__name__
                )
                report = await self._inspect_snapshot(image_path, page)
            except Exception:
                logger.debug("vault/vision fallback skipped")

        if "network" in str(e).lower() or "proxy" in str(e).lower():
            proxy = self.proxy_manager.get_proxy_for_profile(self.profile_id)
            if proxy:
                self.proxy_manager.mark_proxy_failed(self.profile_id, proxy.id)

            await self.hub.broadcast({
                "type": "error_event",
                "metadata": {
                    "profile_id": self.profile_id,
                    "error": type(e).__name__,
                    "type": error_type,
                    "vision": None if report is None else {**report.as_dict(), "profile_id": self.profile_id},
                },
                "timestamp": time.time(),
            })
        return report

    async def _inspect_snapshot(self, image_path, page) -> Any:
        if self.vision is None:
            return None
        try:
            report = await self.vision.analyze_page(image_path)
            await self.hub.broadcast({
                "type": "vision_alert",
                "content": f"[VISION] {report.page_state} ({report.recommended_action})",
                "metadata": {**report.as_dict(), "profile_id": self.profile_id},
                "timestamp": time.time(),
            })
            from robber.agent_loop import get_loop

            loop = get_loop()
            autonomous = loop is None or loop.is_autonomous()
            if report.recommended_action == "pause_and_alert" or not autonomous:
                await self._broadcast_action(
                    f"Operator needed: {report.page_state}"
                )
            elif report.recommended_action == "bypass_modal" and report.page_state == "UNKNOWN_POPUP":
                await self._dismiss_overlay(page)
            elif report.recommended_action == "click_retry" and autonomous:
                await self._broadcast_action("Vision recommended retry")
            return report
        except Exception:
            logger.debug("vision inspector failed closed")
            return None

    async def _dismiss_overlay(self, page) -> None:
        for selector in DISMISS_SELECTORS:
            try:
                locator = page.locator(selector)
                if await locator.count() > 0:
                    await locator.first.click(timeout=1500)
                    await self._broadcast_action("Dismissed overlay via vision fallback")
                    return
            except Exception:
                continue

    async def _broadcast_action(self, message: str):
        payload = {"type": "action_update", "content": f"[ACTION:{self.profile_id}] {message}"}
        await self.hub.broadcast(payload)
        logger.info(message)
        await self.session_manager.update_session(self.profile_id, "metrics", self.metrics)

    async def _execute_with_retry(self, action_func, *args, retries=3, page=None):
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                return await action_func(*args)
            except Exception as e:
                last_error = e
                if attempt == retries - 1:
                    if page is not None:
                        await self._handle_error(e, page, "LOCATOR_FAILED")
                    self.metrics["failures"] += 1
                    await self._broadcast_action(f"Action failed after {retries} attempts")
                    raise e
                await asyncio.sleep(2 ** attempt)
        assert last_error is not None
        raise last_error

    async def _anti_ban_cooldown(self):
        cooldown = random.uniform(5, 15)
        await self._broadcast_action(f"Cooling down for {cooldown:.1f}s to mimic human fatigue")
        await asyncio.sleep(cooldown)

    async def login_account(self, username, password):
        await self._broadcast_action("Logging in...")
        page = None
        try:
            page = self.browser.context.pages[0]
            await page.goto("https://facebook.com")
            await self.browser.human_type(page, 'input[name="email"]', username)
            await self.browser.human_type(page, 'input[name="pass"]', password)
            await page.click('button[name="login"]')
            self.metrics["actions_completed"] += 1
            await self._anti_ban_cooldown()
        except Exception as e:
            await self._handle_error(e, page, "LOGIN_FAILED")
            raise e

    async def interact_post(self, post_selector: str):
        await self._broadcast_action("Executing post interaction...")
        page = None
        try:
            page = self.browser.context.pages[0]
            if await page.query_selector("#captcha"):
                raise Exception("Verification required")
            await self._execute_with_retry(
                self.browser.human_move, page, post_selector, retries=3, page=page
            )
            await page.click(post_selector)
            self.metrics["actions_completed"] += 1
            await self._anti_ban_cooldown()
        except Exception as e:
            error_type = "VERIFICATION_REQUIRED" if "Verification" in str(e) else "INTERACTION_FAILED"
            await self._handle_error(e, page, error_type)
            raise e

    async def scroll_feed(self):
        await self._broadcast_action("Executing feed scroll...")
        await self.browser.context.pages[0].mouse.wheel(0, random.randint(300, 800))
        await self._anti_ban_cooldown()
