import asyncio
import random
import os
from typing import Optional, Any
from playwright.async_api import async_playwright, BrowserContext, Page

class StealthBrowserEngine:
    def __init__(self, profile_id: str, proxy_manager: Optional[Any] = None):
        self.profile_id = profile_id
        self.proxy_manager = proxy_manager
        self.session_path = f"data/browser_sessions/{profile_id}"
        self.context: Optional[BrowserContext] = None
        os.makedirs(self.session_path, exist_ok=True)

    async def get_context(self, playwright):
        proxy_config = None
        if self.proxy_manager:
            proxy_config = self.proxy_manager.get_proxy_for_profile(self.profile_id)
        
        # Configure stealth browser
        proxy_dict = self.proxy_manager.to_playwright_proxy(proxy_config) if proxy_config else None
        
        self.context = await playwright.chromium.launch_persistent_context(
            self.session_path,
            headless=False,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            viewport={"width": random.randint(1200, 1920), "height": random.randint(800, 1080)},
            proxy=proxy_dict,
            args=["--disable-blink-features=AutomationControlled"]
        )
        
        # Apply stealth overrides
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.navigator.chrome = {runtime: {}};
        """)
        return self.context


    async def human_type(self, page: Page, selector: str, text: str):
        """Simulate variable micro-delays between keystrokes."""
        for char in text:
            await page.type(selector, char, delay=random.randint(45, 180))
            await asyncio.sleep(random.uniform(0.01, 0.05))

    async def human_move(self, page: Page, target_selector: str):
        """Simulate natural mouse movement."""
        # This is a simplified approach; true Bézier curves would require a helper library
        # like `puppeteer-extra-plugin-stealth` equivalents for Playwright.
        box = await page.locator(target_selector).bounding_box()
        if box:
            await page.mouse.move(box['x'] + box['width']/2, box['y'] + box['height']/2, steps=10)
            await page.click(target_selector)
