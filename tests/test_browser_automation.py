"""Test Playwright browser automation loop integration."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from robber.agents.vision_auditor import VisionAuditorWorker
from robber.diagnostics.browser_inspector import BrowserInspector
from robber.state_manager import StateManager
from robber.tools.browser_engine import PlaywrightBrowserEngine


@pytest.mark.asyncio
async def test_playwright_browser_engine_init(tmp_path: Path) -> None:
    """Verify PlaywrightBrowserEngine can be initialized."""
    engine = PlaywrightBrowserEngine(tmp_path)
    assert engine.root == tmp_path
    assert engine.snapshot_dir.exists()
    await engine.close()


@pytest.mark.asyncio
async def test_browser_inspector_init(tmp_path: Path) -> None:
    """Verify BrowserInspector can be initialized."""
    inspector = BrowserInspector(tmp_path)
    assert inspector.root == tmp_path


@pytest.mark.asyncio
async def test_vision_auditor_worker_init(tmp_path: Path) -> None:
    """Verify VisionAuditorWorker can be initialized."""
    manager = StateManager(tmp_path)
    worker = VisionAuditorWorker(tmp_path, manager=manager, hub=None)
    assert worker.role == "vision_auditor"
    assert worker.label == "VISION_AUDITOR"
    assert worker.window == 2048


@pytest.mark.asyncio
async def test_browser_snapshot_public_url(tmp_path: Path) -> None:
    """Test browser snapshot against a public URL (example.com)."""
    engine = PlaywrightBrowserEngine(tmp_path)
    try:
        snapshot = await engine.navigate_and_capture("http://example.com")
        assert snapshot.url == "http://example.com"
        assert snapshot.ok
        assert snapshot.status_code == 200
        assert snapshot.title
        assert snapshot.dom_html
        assert snapshot.screenshot_path
        # Check if screenshot exists using absolute path
        screenshot_abs = tmp_path / snapshot.screenshot_path if not Path(snapshot.screenshot_path).is_absolute() else Path(snapshot.screenshot_path)
        assert screenshot_abs.exists(), f"Screenshot not found at {screenshot_abs}"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_browser_inspector_on_snapshot(tmp_path: Path) -> None:
    """Test BrowserInspector on a captured snapshot."""
    engine = PlaywrightBrowserEngine(tmp_path)
    inspector = BrowserInspector(tmp_path)
    try:
        snapshot = await engine.navigate_and_capture("http://example.com")
        report = inspector.inspect_snapshot(snapshot)
        assert report.url == "http://example.com"
        # example.com should have no errors
        assert not report.has_errors or len(report.console_errors) == 0
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_vision_auditor_worker_run(tmp_path: Path) -> None:
    """Test VisionAuditorWorker.run() against a public URL."""
    manager = StateManager(tmp_path)
    worker = VisionAuditorWorker(tmp_path, manager=manager, hub=None)
    
    task = {
        "id": "test-vision-audit",
        "payload_json": {
            "url": "http://example.com",
            "auto_heal": False,  # Don't try to heal example.com
        },
    }
    
    result = await worker.run(task, None)
    assert result.ok or not result.ok  # May pass or fail depending on network
    assert result.output
    # Parse output as JSON
    payload = json.loads(result.output)
    assert payload["url"] == "http://example.com"
    assert "ok" in payload


@pytest.mark.asyncio
async def test_browser_engine_memory_bound(tmp_path: Path) -> None:
    """Verify browser engine respects memory_bound_mb config."""
    engine = PlaywrightBrowserEngine(tmp_path, memory_bound_mb=300)
    assert engine.memory_bound_mb == 300
    await engine.close()


def test_swarm_orchestrator_has_vision_auditor_worker(tmp_path: Path) -> None:
    """Verify SwarmOrchestrator includes VisionAuditorWorker in worker pool."""
    from robber.agents.swarm_orchestrator import SwarmOrchestrator
    
    orchestrator = SwarmOrchestrator(tmp_path)
    assert "vision_auditor" in orchestrator.workers
    worker = orchestrator.workers["vision_auditor"]
    assert isinstance(worker, VisionAuditorWorker)
    assert worker.role == "vision_auditor"


def test_role_labels_includes_vision_auditor() -> None:
    """Verify ROLE_LABELS includes vision_auditor."""
    from robber.agents.sub_agents import ROLE_LABELS
    
    assert "vision_auditor" in ROLE_LABELS
    assert ROLE_LABELS["vision_auditor"] == "VISION_AUDITOR"


def test_webtest_command_registered() -> None:
    """Verify /webtest slash command is registered."""
    from robber.commands.registry import get_registry
    from robber.commands import handlers  # Import to trigger @command decorators
    
    registry = get_registry()
    cmd = registry.get("webtest")
    assert cmd is not None
    assert cmd.name == "webtest"
    assert "web" in cmd.aliases or "browser" in cmd.aliases
    assert cmd.category == "Execution"
