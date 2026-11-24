"""Read-only git history tool used as the sample third-party plugin."""

from __future__ import annotations

import subprocess
from typing import Any

from robber.plugins.base_plugin import BaseAgentPlugin, PluginResult, PluginRuntime, ToolParameter, ToolSchema


class GitHistoryAnalyzer(BaseAgentPlugin):
    name = "git_history_analyzer"
    version = "1.0"
    categories = ("RESEARCH_ANALYSIS", "ARCHITECTURAL_AUDIT")
    keywords = ("git", "history", "commit", "log", "blame")

    def initialize(self, context: PluginRuntime) -> None:
        self._root = context.root

    def get_tool_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description="Summarize recent git commits in the workspace (read-only log).",
            parameters={
                "limit": ToolParameter(
                    type="integer",
                    description="Maximum number of commits to list",
                    required=False,
                    default=12,
                ),
                "path": ToolParameter(
                    type="string",
                    description="Optional path relative to the workspace",
                    required=False,
                    default="",
                ),
            },
        )

    def execute(self, args: dict[str, Any], context: PluginRuntime) -> PluginResult:
        limit = max(1, min(int(args.get("limit") or 12), 50))
        rel = str(args.get("path") or "").replace("\\", "/").strip().lstrip("/")
        argv = ["git", "log", "--oneline", f"-n{limit}"]
        if rel and ".." not in rel.split("/"):
            argv.extend(["--", rel])
        try:
            proc = subprocess.run(
                argv,
                cwd=str(context.root),
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return PluginResult(ok=False, output=f"git log failed: {type(exc).__name__}", exit_code=1)
        commits = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()][:limit]
        data = {
            "commits": commits,
            "count": len(commits),
            "exit_code": int(proc.returncode),
            "path": rel,
        }
        if proc.returncode != 0 and not commits:
            data["stderr"] = (proc.stderr or "")[:400]
            return PluginResult(
                ok=True,
                output="git history unavailable",
                data=data,
                exit_code=0,
            )
        output = "\n".join(commits) if commits else "no commits"
        return PluginResult(ok=True, output=output, data=data, exit_code=0)

    def teardown(self) -> None:
        return None
