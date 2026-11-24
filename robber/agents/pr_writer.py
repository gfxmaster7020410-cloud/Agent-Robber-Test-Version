"""PRWriterWorker — AST diffs and execution logs into a structured PR body."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from robber.agents.sub_agents import WorkerAgent, WorkerResult
from robber.context_assembly import merge_payload
from robber.ssot_context import estimate_tokens
from robber.tools.git_manager import GitManager, slug_from_query

logger = logging.getLogger(__name__)

SWARM_PR_TOKENS = 2048


@dataclass
class SymbolChange:
    name: str
    kind: str = "modified"  # added | modified | removed
    path: str = ""


@dataclass
class PRDocument:
    title: str
    summary: str
    symbols: list[SymbolChange] = field(default_factory=list)
    tests: str = ""
    breaking: str = ""
    branch: str = ""
    body: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "symbols": [asdict(item) for item in self.symbols],
            "tests": self.tests,
            "breaking": self.breaking,
            "branch": self.branch,
            "body": self.body,
        }


class PRWriterWorker(WorkerAgent):
    """Turn swarm artifacts + git diffs into a production PR description."""

    role = "pr_writer"
    label = "PR_WRITER"
    window = SWARM_PR_TOKENS
    system_prompt = (
        "You write production-grade pull request descriptions from AST diffs, "
        "test logs, and Playwright snapshots. Be precise and structured."
    )

    def synthesize(
        self,
        *,
        query: str = "",
        files: list[str] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        commit_message: str = "",
        branch: str = "",
        snapshots: list[str] | None = None,
    ) -> PRDocument:
        files = [str(item) for item in (files or []) if str(item)]
        git = GitManager(self.root, manager=self.manager)
        if not files:
            files = git.changed_files()
        branch = branch or git.current_branch()
        symbols = self._symbol_changes(files)
        tests = self._test_summary(artifacts or [])
        snapshots = snapshots or self._snapshot_paths(artifacts or [])
        breaking = self._breaking_notes(files, symbols)
        title = commit_message.strip() or self._title_from_query(query, files)
        summary = self._summary(query, files, symbols)
        body = self._render(
            summary=summary,
            symbols=symbols,
            tests=tests,
            breaking=breaking,
            snapshots=snapshots,
            files=files,
            branch=branch,
            title=title,
        )
        return PRDocument(
            title=title,
            summary=summary,
            symbols=symbols,
            tests=tests,
            breaking=breaking,
            branch=branch,
            body=body,
        )

    async def run(self, task: dict[str, Any], context: Any) -> WorkerResult:
        payload = merge_payload(task.get("payload_json"))
        query = str(payload.get("query") or task.get("title") or "")
        files = [str(item) for item in (payload.get("files") or payload.get("inputs") or []) if str(item)]
        artifacts: list[dict[str, Any]] = []
        if self.manager is not None:
            try:
                artifacts = self.manager.list_artifacts(task_id=str(task.get("id") or "") or None)
            except Exception:
                artifacts = []
        await self.emit("synthesizing pull request body")
        doc = self.synthesize(
            query=query,
            files=files,
            artifacts=artifacts,
            commit_message=str(payload.get("commit_message") or ""),
            branch=str(payload.get("branch") or ""),
        )
        if self.manager is not None:
            try:
                self.manager.add_artifact(
                    "pr:body",
                    body=doc.body[:4000],
                    kind="pr",
                    task_id=str(task.get("id") or "") or None,
                    exit_code=0,
                )
            except Exception:
                logger.debug("pr body artifact skipped")
        packed = json.dumps(doc.as_dict(), ensure_ascii=True)
        return WorkerResult(ok=True, output=doc.body, tokens=estimate_tokens(packed), payload=doc.as_dict())

    def _symbol_changes(self, files: list[str]) -> list[SymbolChange]:
        git = GitManager(self.root)
        changes: list[SymbolChange] = []
        seen: set[str] = set()
        for rel in files[:16]:
            dest = self.root / rel
            ours = dest.read_text(encoding="utf-8", errors="replace") if dest.is_file() else ""
            base = git._show("HEAD", rel) if git.is_repo() else ""
            added = _names(ours) - _names(base)
            removed = _names(base) - _names(ours)
            kept = _names(ours) & _names(base)
            for name in sorted(added):
                key = f"add:{rel}:{name}"
                if key not in seen:
                    seen.add(key)
                    changes.append(SymbolChange(name=name, kind="added", path=rel))
            for name in sorted(removed):
                key = f"del:{rel}:{name}"
                if key not in seen:
                    seen.add(key)
                    changes.append(SymbolChange(name=name, kind="removed", path=rel))
            for name in sorted(kept):
                if _block(ours, name) != _block(base, name):
                    key = f"mod:{rel}:{name}"
                    if key not in seen:
                        seen.add(key)
                        changes.append(SymbolChange(name=name, kind="modified", path=rel))
        return changes[:24]

    def _test_summary(self, artifacts: list[dict[str, Any]]) -> str:
        rows: list[str] = []
        for item in artifacts:
            kind = str(item.get("kind") or "")
            name = str(item.get("name") or "")
            if kind not in {"test", "profile"} and not name.startswith("test:"):
                continue
            body = str(item.get("body") or "")
            try:
                packed = json.loads(body) if body.startswith("{") else {}
            except json.JSONDecodeError:
                packed = {}
            ok = packed.get("ok") if isinstance(packed, dict) else None
            if ok is None:
                ok = int(item.get("exit_code") or 1) == 0
            status = "pass" if ok else "fail"
            rows.append(f"- {name or kind}: **{status}**")
        if not rows:
            return "- No automated test artifacts recorded for this task."
        return "\n".join(rows[:12])

    def _snapshot_paths(self, artifacts: list[dict[str, Any]]) -> list[str]:
        found: list[str] = []
        for item in artifacts:
            if str(item.get("kind") or "") not in {"vision", "snapshot"}:
                continue
            body = str(item.get("body") or "")
            try:
                packed = json.loads(body) if body.startswith("{") else {}
            except json.JSONDecodeError:
                packed = {}
            for key in ("screenshot_after", "screenshot_before", "screenshot"):
                path = packed.get(key) if isinstance(packed, dict) else None
                if path:
                    found.append(str(path))
        return found

    def _breaking_notes(self, files: list[str], symbols: list[SymbolChange]) -> str:
        removed = [item.name for item in symbols if item.kind == "removed"]
        schema = [rel for rel in files if "migrat" in rel.lower() or rel.endswith(".sql")]
        if not removed and not schema:
            return "None."
        lines: list[str] = []
        if removed:
            lines.append("Removed symbols: " + ", ".join(removed[:8]))
        if schema:
            lines.append("Schema / migration files: " + ", ".join(schema[:6]))
        return "\n".join(f"- {line}" for line in lines)

    def _summary(self, query: str, files: list[str], symbols: list[SymbolChange]) -> str:
        added = [item.name for item in symbols if item.kind == "added"]
        if added:
            return (
                f"Implements {query.strip()[:160] or 'the requested change'} "
                f"by adding {', '.join(added[:5])}"
                + (f" across {len(files)} file(s)." if files else ".")
            )
        if files:
            return f"Updates {', '.join(files[:6])} for: {query.strip()[:160] or 'task scope'}."
        return query.strip()[:240] or "Automated Agent Robber change set."

    def _title_from_query(self, query: str, files: list[str]) -> str:
        slug = slug_from_query(query)
        scope = Path(files[0]).stem if files else slug.split("-")[0]
        label = re.sub(r"[-_]+", " ", slug).strip() or "agent change"
        return f"feat({scope}): {label}"

    def _render(
        self,
        *,
        summary: str,
        symbols: list[SymbolChange],
        tests: str,
        breaking: str,
        snapshots: list[str],
        files: list[str],
        branch: str,
        title: str,
    ) -> str:
        symbol_lines = (
            "\n".join(f"- `{item.kind}` `{item.name}` ({item.path})" for item in symbols)
            if symbols
            else "- No AST symbol delta detected."
        )
        snap_lines = (
            "\n".join(f"- `{path}`" for path in snapshots)
            if snapshots
            else "- No Playwright snapshots attached."
        )
        file_lines = "\n".join(f"- `{rel}`" for rel in files[:16]) or "- (working tree)"
        return "\n".join(
            [
                f"## Summary of Changes",
                summary,
                "",
                f"## AST Symbol Changes",
                symbol_lines,
                "",
                f"## Verification & Test Coverage",
                tests,
                "",
                f"### Playwright snapshots",
                snap_lines,
                "",
                f"## Breaking Changes & Migration Notes",
                breaking,
                "",
                f"## Files",
                file_lines,
                "",
                f"_Branch `{branch or 'HEAD'}` · {title}_",
            ]
        )


def _names(src: str) -> set[str]:
    return set(re.findall(r"^(?:def|class|fn|func|export function|export class)\s+(\w+)", src or "", re.M))


def _block(src: str, name: str) -> str:
    match = re.search(
        rf"^(?:def|class|fn|func|export function|export class)\s+{re.escape(name)}\b.*",
        src or "",
        re.M,
    )
    return match.group(0) if match else ""
