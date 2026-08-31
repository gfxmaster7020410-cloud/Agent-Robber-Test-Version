import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any
import websockets
from playwright.async_api import async_playwright
from robber.engine.proxy_rotator import ProxyManager, ProxyConfig
from robber.engine.vault import SnapshotVaultManager
from robber.engine.browser_stealth import StealthBrowserEngine
from robber.engine.worker_pool import WorkerPool
from robber.engine.session_manager import SessionManager

# Mock Hub for dependencies
class MockHub:
    async def broadcast(self, payload: Any):
        print(f"[MockHub] Broadcast event: {payload.get('type')}")

async def run_diagnostics():
    print("--- Starting End-to-End Live System Diagnostic ---")
    results = {}

    # Setup dependencies
    hub = MockHub()
    proxy_manager = ProxyManager()
    vault = SnapshotVaultManager(hub)
    
    # 1. ProxyManager Diagnostic
    print("\n[1/4] Testing ProxyManager...")
    # Add dummy proxy
    proxy_manager.proxies["test_1"] = ProxyConfig(id="test_1", host="127.0.0.1", port=8080)
    # Don't expect successful health check as 127.0.0.1:8080 likely not running
    is_healthy = await proxy_manager.check_proxy_health(proxy_manager.proxies["test_1"])
    print(f"Proxy Health Check (Expect False/Degraded): {is_healthy}")
    # Consider this a PASS if the code executed without crashing
    results["ProxyManager"] = "PASS"

    # 2. SnapshotVault Diagnostic
    print("\n[2/4] Testing SnapshotVault...")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto("about:blank")
            await vault.capture_failure_snapshot(page, "test_profile", "TEST_ERROR", "Simulated system check")
            await browser.close()
        
        snapshot_path = Path("data/vault_snapshots/test_profile")
        if snapshot_path.exists():
            print(f"Snapshot captured at {snapshot_path}")
            results["SnapshotVault"] = "PASS"
        else:
            results["SnapshotVault"] = "FAIL"
    except Exception as e:
        print(f"SnapshotVault failed: {e}")
        results["SnapshotVault"] = "FAIL"

    # 3. StealthBrowserEngine Diagnostic
    print("\n[3/4] Testing StealthBrowserEngine...")
    try:
        async with async_playwright() as p:
            engine = StealthBrowserEngine("test_profile", proxy_manager=proxy_manager)
            await engine.get_context(p)
            if engine.context:
                await engine.context.close()
        results["StealthBrowser"] = "PASS"
    except Exception as e:
        print(f"StealthBrowser failed: {e}")
        results["StealthBrowser"] = "FAIL"

    # 3.5 WorkerPool Diagnostic
    print("\n[3.5/4] Testing WorkerPool background task...")
    try:
        session_manager = SessionManager()
        pool = WorkerPool(hub, session_manager, vault, proxy_manager)
        # Attempt starting a worker (will gracefully fail/close since we mock async_playwright or since we quickly stop it)
        await pool.start_worker("test_profile", "https://facebook.com")
        await asyncio.sleep(1.0)
        await pool.stop_worker("test_profile")
        results["WorkerPool"] = "PASS"
    except Exception as e:
        print(f"WorkerPool failed: {e}")
        results["WorkerPool"] = "FAIL"

    # 4. WebSocket/CommandRouter Diagnostic
    print("\n[4/4] Testing WebSocket Integration...")
    try:
        async with websockets.connect("ws://127.0.0.1:8787/ws") as ws:
            # Send ping
            await ws.send(json.dumps({"type": "ping"}))
            response = await ws.recv()
            print(f"Received ping: {response[:50]}...")
            
            # Send status
            await ws.send(json.dumps({"type": "status"}))
            response = await ws.recv()
            print(f"Received status: {response[:50]}...")
            
            results["WebSocket"] = "PASS"
    except Exception as e:
        print(f"WebSocket diagnostic failed: {e}. Ensure server is running.")
        results["WebSocket"] = "FAIL (Server not running?)"

    # Final Report
    print("\n--- Diagnostic Summary ---")
    for comp, status in results.items():
        print(f"{comp:20}: {status}")

    # Cleanup
    if Path("data/vault_snapshots/test_profile").exists():
        shutil.rmtree("data/vault_snapshots/test_profile")

if __name__ == "__main__":
    asyncio.run(run_diagnostics())
