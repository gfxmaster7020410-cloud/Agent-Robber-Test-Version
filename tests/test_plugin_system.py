"""Plugin SDK, hot-reload, SSOT artifact safety, and benchmark metrics."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from robber.agent_loop import AutonomousAgentLoop
from robber.commands.registry import ExecutionContext, get_registry
from robber.plugins.base_plugin import PluginError
from robber.plugins.plugin_loader import get_loader, reset_loaders
from robber.plugins.safety import file_digest, validate_plugin_file
from robber.planner import AutonomousPlanner
from robber.ssot_db import SCHEMA_VERSION, SsotDB
from robber.state_manager import StateManager

import robber.commands.handlers  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "robber" / "plugins" / "samples" / "git_history_analyzer.py"

ECHO_V1 = """from robber.plugins.base_plugin import BaseAgentPlugin, PluginResult, PluginRuntime, ToolSchema

class EchoTool(BaseAgentPlugin):
    name = "echo_tool"
    def get_tool_schema(self) -> ToolSchema:
        return ToolSchema(name="echo_tool", description="echo marker")
    def execute(self, args, context: PluginRuntime) -> PluginResult:
        return PluginResult(ok=True, output="marker-v1")
"""

ECHO_V2 = ECHO_V1.replace("marker-v1", "marker-v2")


def _ctx(tmp_path: Path, args: list[str] | None = None) -> ExecutionContext:
    host = type("H", (), {})()
    return ExecutionContext(
        args=list(args or []),
        session_id="plugin-test",
        workspace=tmp_path,
        state={},
        host=host,  # type: ignore[arg-type]
    )


def _install_sample(tmp_path: Path) -> Path:
    dest = tmp_path / ".robber" / "plugins"
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / SAMPLE.name
    shutil.copy(SAMPLE, target)
    reset_loaders(tmp_path)
    return target


def test_sample_plugin_hot_reload_and_ssot(tmp_path: Path) -> None:
    plugin_path = _install_sample(tmp_path)
    loader = get_loader(tmp_path)
    loaded = loader.get("git_history_analyzer")
    assert loaded is not None
    schema = loaded.get_tool_schema()
    assert schema.name == "git_history_analyzer"
    result = loader.execute("git_history_analyzer", {"limit": 5})
    assert result.ok
    db = SsotDB(tmp_path / ".robber" / "ssot.db").connect()
    assert db.integrity_check().lower() == "ok"
    assert str(db.snapshot().get("version")) == SCHEMA_VERSION
    arts = db.list_artifacts()
    db.close()
    assert any(row.get("kind") == "plugin" and row.get("name") == "git_history_analyzer" for row in arts)

    echo = tmp_path / ".robber" / "plugins" / "echo_tool.py"
    echo.write_text(ECHO_V1, encoding="utf-8")
    loader.reload()
    first = loader.execute("echo_tool", {})
    assert first.output == "marker-v1"
    echo.write_text(ECHO_V2, encoding="utf-8")
    loader.reload()
    second = loader.execute("echo_tool", {})
    assert second.output == "marker-v2"
    assert plugin_path.is_file()


def test_rejects_eval_and_bad_signature(tmp_path: Path) -> None:
    plugins = tmp_path / ".robber" / "plugins"
    plugins.mkdir(parents=True)
    evil = plugins / "evil_tool.py"
    evil.write_text(
        "from robber.plugins.base_plugin import BaseAgentPlugin, PluginResult, ToolSchema\n"
        "class Evil(BaseAgentPlugin):\n"
        "    name = 'evil_tool'\n"
        "    def get_tool_schema(self):\n"
        "        return ToolSchema(name='evil_tool', description='x')\n"
        "    def execute(self, args, context):\n"
        "        return PluginResult(ok=True, output=str(eval('1')))\n",
        encoding="utf-8",
    )
    reset_loaders(tmp_path)
    loader = get_loader(tmp_path)
    assert loader.get("evil_tool") is None
    assert any("eval" in item or "not allowed" in item for item in loader.errors)

    good = plugins / "echo_tool.py"
    good.write_text(ECHO_V1, encoding="utf-8")
    sidecar = Path(str(good) + ".sha256")
    sidecar.write_text("0" * 64, encoding="ascii")
    reset_loaders(tmp_path)
    loader = get_loader(tmp_path)
    assert loader.get("echo_tool") is None
    assert any("signature" in item for item in loader.errors)
    sidecar.write_text(file_digest(good), encoding="ascii")
    assert validate_plugin_file(tmp_path, good) == file_digest(good)


def test_malformed_args_rejected(tmp_path: Path) -> None:
    _install_sample(tmp_path)
    loader = get_loader(tmp_path)
    with pytest.raises(PluginError):
        loader.execute("git_history_analyzer", {"limit": "nope"})


def test_slash_plugin_reload(tmp_path: Path) -> None:
    _install_sample(tmp_path)

    async def go() -> None:
        result = await get_registry().dispatch("plugin", _ctx(tmp_path, ["reload"]))
        assert result.ok
        assert "Reloaded" in result.content
        listed = await get_registry().dispatch("plugin", _ctx(tmp_path, ["list"]))
        assert listed.ok
        assert "git_history_analyzer" in listed.content

    asyncio.run(go())


def test_plugin_in_planner_and_simulated_loop(tmp_path: Path) -> None:
    _install_sample(tmp_path)
    prompt = "summarize git history of this workspace"
    plan = AutonomousPlanner(tmp_path).plan(prompt)
    tags = {task.tag for task in plan.tasks}
    names = [str((task.payload or {}).get("plugin") or "") for task in plan.tasks]
    assert "plugin" in tags
    assert "git_history_analyzer" in names

    async def go() -> None:
        loop = AutonomousAgentLoop(tmp_path)
        report = await loop.run(prompt, max_steps=8)
        assert report.steps >= 1
        assert not report.failed

    asyncio.run(go())
    arts = StateManager(tmp_path).list_artifacts(limit=32)
    assert any(row.get("kind") == "plugin" for row in arts)


def test_benchmark_cycle_records_kpis(tmp_path: Path) -> None:
    from benchmarks.run_benchmarks import run_suite

    dest = tmp_path / "results.json"
    report = run_suite(tmp_path / "bench-ws", out=dest)
    assert dest.is_file()
    payload = json.loads(dest.read_text(encoding="utf-8"))
    assert payload["success_rate"] == 1.0
    assert payload["rss_ok"] is True
    assert payload["peak_rss_bytes"] < payload["rss_limit_bytes"]
    assert payload["tokens"] > 0
    assert "plan_ms" in payload["latency_ms"]
    assert payload["tasks_total"] == 3
    assert report["success_rate"] == 1.0
