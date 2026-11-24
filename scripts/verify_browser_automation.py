#!/usr/bin/env python3
"""
Verification script for Autonomous Visual UI Debugging & Playwright Browser Automation Loop.

This script performs a comprehensive system check to ensure all modules are properly
installed, configured, and operational.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


async def main() -> int:
    print("=" * 80)
    print("Agent Robber - Browser Automation Verification")
    print("=" * 80)
    print()

    checks_passed = 0
    checks_total = 0

    # Check 1: Playwright installation
    checks_total += 1
    print("[1/9] Checking Playwright installation...")
    try:
        from playwright.async_api import async_playwright
        print("  [OK] Playwright installed")
        checks_passed += 1
    except ImportError as exc:
        print(f"  [FAIL] Playwright not installed: {exc}")
        print("    Run: pip install playwright>=1.40")
        print("    Then: playwright install chromium")

    # Check 2: Browser engine module
    checks_total += 1
    print("[2/9] Checking browser_engine module...")
    try:
        from robber.tools.browser_engine import PlaywrightBrowserEngine, BrowserSnapshot
        print("  [OK] browser_engine module OK")
        checks_passed += 1
    except ImportError as exc:
        print(f"  [FAIL] browser_engine import failed: {exc}")

    # Check 3: Browser inspector module
    checks_total += 1
    print("[3/9] Checking browser_inspector module...")
    try:
        from robber.diagnostics.browser_inspector import BrowserInspector, BrowserInspectionReport
        print("  [OK] browser_inspector module OK")
        checks_passed += 1
    except ImportError as exc:
        print(f"  [FAIL] browser_inspector import failed: {exc}")

    # Check 4: Vision auditor worker
    checks_total += 1
    print("[4/9] Checking vision_auditor worker...")
    try:
        from robber.agents.vision_auditor import VisionAuditorWorker
        print("  [OK] vision_auditor module OK")
        checks_passed += 1
    except ImportError as exc:
        print(f"  [FAIL] vision_auditor import failed: {exc}")

    # Check 5: SwarmOrchestrator integration
    checks_total += 1
    print("[5/9] Checking SwarmOrchestrator integration...")
    try:
        from robber.agents.swarm_orchestrator import SwarmOrchestrator
        orchestrator = SwarmOrchestrator()
        if "vision_auditor" in orchestrator.workers:
            print("  [OK] VisionAuditorWorker registered in swarm")
            checks_passed += 1
        else:
            print("  [FAIL] VisionAuditorWorker not found in swarm workers")
    except Exception as exc:
        print(f"  [FAIL] SwarmOrchestrator check failed: {exc}")

    # Check 6: Role labels
    checks_total += 1
    print("[6/9] Checking ROLE_LABELS...")
    try:
        from robber.agents.sub_agents import ROLE_LABELS
        if "vision_auditor" in ROLE_LABELS:
            print(f"  [OK] vision_auditor role label: {ROLE_LABELS['vision_auditor']}")
            checks_passed += 1
        else:
            print("  [FAIL] vision_auditor not in ROLE_LABELS")
    except Exception as exc:
        print(f"  [FAIL] ROLE_LABELS check failed: {exc}")

    # Check 7: Slash command registration
    checks_total += 1
    print("[7/9] Checking /webtest command registration...")
    try:
        from robber.commands.registry import get_registry
        from robber.commands import handlers  # Trigger @command decorators
        registry = get_registry()
        cmd = registry.get("webtest")
        if cmd is not None:
            print(f"  [OK] /webtest command registered (aliases: {', '.join(cmd.aliases)})")
            checks_passed += 1
        else:
            print("  [FAIL] /webtest command not registered")
    except Exception as exc:
        print(f"  [FAIL] Command registration check failed: {exc}")

    # Check 8: Configuration
    checks_total += 1
    print("[8/9] Checking browser configuration...")
    try:
        from robber.config import (
            BROWSER_MEMORY_BOUND_MB,
            BROWSER_SNAPSHOT_DIR,
            BROWSER_VIEWPORT,
            BROWSER_TIMEOUT_SECONDS,
        )
        print(f"  [OK] Memory bound: {BROWSER_MEMORY_BOUND_MB} MB")
        print(f"  [OK] Snapshot dir: {BROWSER_SNAPSHOT_DIR}")
        print(f"  [OK] Viewport: {BROWSER_VIEWPORT}")
        print(f"  [OK] Timeout: {BROWSER_TIMEOUT_SECONDS}s")
        checks_passed += 1
    except ImportError as exc:
        print(f"  [FAIL] Configuration import failed: {exc}")

    # Check 9: Live browser test
    checks_total += 1
    print("[9/9] Running live browser test (example.com)...")
    try:
        from robber.tools.browser_engine import PlaywrightBrowserEngine
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = PlaywrightBrowserEngine(Path(tmpdir))
            try:
                snapshot = await engine.navigate_and_capture("http://example.com")
                if snapshot.ok and snapshot.status_code == 200:
                    print(f"  [OK] Browser test passed (title: {snapshot.title})")
                    print(f"    Screenshot: {snapshot.screenshot_path}")
                    print(f"    DOM size: {len(snapshot.dom_html)} bytes")
                    print(f"    Elapsed: {snapshot.elapsed_ms}ms")
                    checks_passed += 1
                else:
                    print(f"  [FAIL] Browser test failed (status: {snapshot.status_code})")
            finally:
                await engine.close()
    except Exception as exc:
        print(f"  [FAIL] Live browser test failed: {exc}")

    print()
    print("=" * 80)
    print(f"Verification Results: {checks_passed}/{checks_total} checks passed")
    print("=" * 80)

    if checks_passed == checks_total:
        print()
        print("[SUCCESS] ALL CHECKS PASSED - Browser automation is ready for use!")
        print()
        print("Try it now:")
        print("  python robber/launcher.py --skip-build")
        print("  Then in TUI: /webtest http://example.com")
        print()
        return 0
    else:
        print()
        print(f"[FAIL] {checks_total - checks_passed} check(s) failed - please review errors above")
        print()
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
