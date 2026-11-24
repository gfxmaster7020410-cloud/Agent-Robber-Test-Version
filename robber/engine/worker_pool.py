import asyncio
import logging
import random
from typing import Any, Dict, Optional

from playwright.async_api import async_playwright

from robber.engine.action_orchestrator import ActionOrchestrator
from robber.engine.browser_stealth import StealthBrowserEngine

logger = logging.getLogger(__name__)


class WorkerPool:
    def __init__(
        self,
        hub,
        session_manager,
        vault,
        proxy_manager,
        account_manager=None,
        vision_inspector=None,
        healer=None,
    ):
        self.hub = hub
        self.session_manager = session_manager
        self.vault = vault
        self.proxy_manager = proxy_manager
        self.account_manager = account_manager
        self.vision_inspector = vision_inspector
        self.healer = healer
        self.workers: Dict[str, asyncio.Task] = {}
        self.worker_states: Dict[str, Dict[str, Any]] = {}

    def is_running(self, profile_id: str) -> bool:
        return profile_id in self.workers

    async def start_worker(self, profile_id: str, target: str):
        if profile_id in self.workers:
            return

        self.worker_states[profile_id] = {"target": target, "status": "running"}
        if self.account_manager:
            try:
                await self.account_manager.set_status(profile_id, "running")
                await self.account_manager.log_execution(profile_id, "start_worker", "ok", target)
            except Exception:
                logger.debug("account status skip for %s", profile_id)
        if self.proxy_manager is not None:
            self.proxy_manager.assign_to_profile(profile_id)
        task = asyncio.create_task(self._worker_loop(profile_id, target))
        self.workers[profile_id] = task
        await self._broadcast_log(f"Worker {profile_id} started for {target}")
        await self._broadcast_tick(profile_id, "started", target)

    async def stop_worker(self, profile_id: str):
        task = self.workers.get(profile_id)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await self._broadcast_log(f"Worker {profile_id} stopping...")
        target = (self.worker_states.get(profile_id) or {}).get("target", "")
        await self._broadcast_tick(profile_id, "stopping", str(target))

    async def _worker_loop(self, profile_id: str, target: str):
        engine = StealthBrowserEngine(profile_id, proxy_manager=self.proxy_manager)
        try:
            await self._broadcast_log(f"[STEALTH] Launching stealth browser for {profile_id}...")
            async with async_playwright() as playwright:
                context = await engine.get_context(playwright)
                await self._restore_cookies(context, profile_id)
                orchestrator = ActionOrchestrator(
                    engine,
                    profile_id,
                    self.hub,
                    self.session_manager,
                    self.vault,
                    self.proxy_manager,
                    vision_inspector=self.vision_inspector,
                    healer=self.healer,
                )
                try:
                    while True:
                        try:
                            if self.healer is not None:
                                await self.healer.run_with_heal(
                                    f"worker:{profile_id}:scroll",
                                    orchestrator.scroll_feed,
                                )
                            else:
                                await orchestrator.scroll_feed()
                            await self._broadcast_tick(profile_id, "ok", target)
                        except Exception as tick_exc:
                            await self._broadcast_log(
                                f"[WORKER:{profile_id}] tick failed ({type(tick_exc).__name__})",
                                level="ERROR",
                            )
                            await self._broadcast_tick(
                                profile_id, "error", target, error=type(tick_exc).__name__
                            )
                        await self._persist_cookies(context, profile_id)
                        await asyncio.sleep(random.uniform(5, 15))
                finally:
                    await self._persist_cookies(context, profile_id)
                    await context.close()
        except asyncio.CancelledError:
            await self._broadcast_log(f"[WORKER:{profile_id}] Task cancelled.")
            await self._safe_account_status(profile_id, "idle", "stop_worker", "cancelled")
        except Exception as exc:
            await self._broadcast_log(f"[WORKER:{profile_id}] Error: {type(exc).__name__}", level="ERROR")
            await self._safe_account_status(
                profile_id, "error", "worker_loop", type(exc).__name__, log_status="error"
            )
        finally:
            self.workers.pop(profile_id, None)
            self.worker_states.pop(profile_id, None)
            await self._broadcast_log(f"Worker {profile_id} resources cleaned up.")

    async def _restore_cookies(self, context, profile_id: str) -> None:
        if self.account_manager is None:
            return
        cookies = await self.account_manager.load_session_cookies(profile_id)
        playable = [cookie for cookie in cookies if cookie.get("name") and cookie.get("value") is not None]
        if not playable:
            return
        try:
            await context.add_cookies(playable)
            await self.account_manager.log_execution(
                profile_id, "load_session_cookies", "ok", f"{len(playable)} cookies"
            )
        except Exception:
            await self.account_manager.log_execution(
                profile_id, "load_session_cookies", "error", "playwright rejected cookie payload"
            )

    async def _persist_cookies(self, context, profile_id: str) -> None:
        if self.account_manager is None:
            return
        try:
            cookies = await context.cookies()
            await self.account_manager.save_session_cookies(profile_id, cookies)
        except Exception:
            logger.debug("cookie persist skipped for %s", profile_id)

    async def _safe_account_status(
        self,
        profile_id: str,
        status: str,
        action: str,
        message: str,
        log_status: str = "ok",
    ) -> None:
        if self.account_manager is None:
            return
        try:
            await self.account_manager.set_status(profile_id, status)
            await self.account_manager.log_execution(profile_id, action, log_status, message)
        except Exception:
            logger.debug("account status skip for %s", profile_id)

    async def _broadcast_log(self, message: str, level: str = "INFO"):
        payload = {"type": "log", "content": message, "metadata": {"level": level}}
        if self.hub:
            await self.hub.broadcast(payload)
        logger.info(message)

    async def _broadcast_tick(
        self,
        profile_id: str,
        status: str,
        target: str,
        **extra: Any,
    ) -> None:
        from robber.agent_loop import suite_event

        payload = suite_event(
            "worker_tick",
            f"[WORKER:{profile_id}] {status}",
            {"profile_id": profile_id, "target": target, "status": status, **extra},
        )
        if self.hub:
            await self.hub.broadcast(payload)

    async def stop_all(self) -> None:
        for profile_id in list(self.workers):
            await self.stop_worker(profile_id)
