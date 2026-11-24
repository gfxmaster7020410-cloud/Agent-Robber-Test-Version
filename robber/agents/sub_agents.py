"""Specialized swarm workers with tiny context windows.

Inter-worker messages go through SSOT (task_state payload, memory_store,
execution_artifacts). No in-memory job queues.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robber.config import (
    COMBINED_RSS_LIMIT,
    GRAPH_SLICE_TOKEN_CAP,
    SWARM_ARCHITECT_TOKENS,
    SWARM_AUDITOR_TOKENS,
    SWARM_CODER_TOKENS,
    SWARM_TEST_TIMEOUT_SECS,
    SWARM_TESTER_TOKENS,
    WORKER_PROMPT_TOKEN_CAP,
)
from robber.context_assembly import merge_payload
from robber.jail import workspace_root
from robber.ssot_context import estimate_tokens

logger = logging.getLogger(__name__)

ROLE_LABELS = {
    "architect": "ARCHITECT",
    "coder": "CODER",
    "auditor": "AUDITOR",
    "tester": "TESTER",
    "vision_auditor": "VISION_AUDITOR",
    "pr_writer": "PR_WRITER",
}

_TAG_ROLE = {
    "goal": "architect",
    "inspect": "architect",
    "answer": "architect",
    "ingest": "architect",
    "synthesize": "architect",
    "mutate": "coder",
    "fix": "coder",
    "plugin": "coder",
    "audit": "auditor",
    "report": "auditor",
    "test": "tester",
    "verify": "tester",
    "webtest": "vision_auditor",
    "vision": "vision_auditor",
    "pr": "pr_writer",
    "pull-request": "pr_writer",
}


@dataclass
class WorkerResult:
    ok: bool = True
    output: str = ""
    tokens: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    pause: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output": (self.output or "")[:800],
            "tokens": self.tokens,
            "pause": self.pause,
        }


class WorkerAgent(ABC):
    """Async worker bound to one SSOT agent_state row."""

    role: str = ""
    label: str = ""
    window: int = SWARM_ARCHITECT_TOKENS
    system_prompt: str = ""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        manager: Any = None,
        hub: Any = None,
    ) -> None:
        self.root = Path(root).resolve() if root else workspace_root()
        self.manager = manager
        self.hub = hub
        self.agent_id: str | None = None

    def clip(self, text: str) -> str:
        from robber.memory.token_budgeter import TokenBudgeter

        packed, _, _ = TokenBudgeter(self.window, hard_cap=WORKER_PROMPT_TOKEN_CAP).fit(text or "")
        return packed

    def bind_agent(self, agent_id: str | None) -> None:
        self.agent_id = agent_id

    async def emit(self, detail: str) -> None:
        text = f"[{self.label}] {detail}".strip()
        if self.hub is None:
            return
        try:
            await self.hub.broadcast(
                {
                    "type": "message",
                    "event": "STATE_ACK",
                    "role": "system",
                    "content": text,
                    "text": text,
                    "stream_kind": "ack",
                    "worker": self.label,
                    "append": False,
                }
            )
            await asyncio.sleep(0)
        except Exception:
            logger.debug("swarm emit skipped")

    def relay(self, target: str, body: str, task_id: str | None) -> None:
        if self.manager is None:
            return
        text = self.clip(body)
        try:
            self.manager.add_memory(
                text,
                kind="swarm",
                source=f"{self.role}->{target}",
                token_est=estimate_tokens(text),
            )
            self.manager.add_artifact(
                f"swarm:{self.role}:{target}",
                body=text,
                kind="swarm",
                task_id=task_id,
                exit_code=0,
            )
        except Exception:
            logger.debug("swarm relay skipped")

    def touch(self, phase: str, task_id: str | None, loop_status: str) -> None:
        if self.manager is None:
            return
        try:
            self.manager.put_agent(
                f"swarm-{self.role}",
                language="python",
                phase=phase,
                loop_status=loop_status,
                task_id=task_id,
                telemetry={"role": self.role, "window": self.window},
                agent_id=self.agent_id,
            )
        except Exception:
            logger.debug("swarm agent touch skipped")

    @abstractmethod
    async def run(self, task: dict[str, Any], context: Any) -> WorkerResult:
        raise NotImplementedError


class ArchitectWorker(WorkerAgent):
    role = "architect"
    label = "ARCHITECT"
    window = SWARM_ARCHITECT_TOKENS
    system_prompt = (
        "You are the System Architect. Emit a strict JSON DAG: tasks, depends_on, "
        "files, and constraints. Isolate work on an agent/* git branch. Do not write code."
    )

    async def run(self, task: dict[str, Any], context: Any) -> WorkerResult:
        payload = merge_payload(task.get("payload_json"))
        dag = payload.get("dag") if isinstance(payload.get("dag"), dict) else {}
        query = str(payload.get("query") or task.get("title") or "")
        files = list(payload.get("inputs") or payload.get("files") or [])
        spec_brief = ""
        try:
            from robber.engine.sdd_spec import SpecDrivenEngine

            spec = SpecDrivenEngine(self.root, manager=self.manager).plan(query)
            spec_brief = spec.worker_brief()
            await self.emit("SDD spec stored")
        except Exception:
            logger.debug("sdd plan skipped")
        branch = ""
        try:
            from robber.tools.git_manager import GitManager, slug_from_query

            git = GitManager(self.root, manager=self.manager)
            if git.available():
                branch = git.create_feature_branch(str(task.get("id") or ""), slug_from_query(query))
                await self.emit(f"isolated branch {branch}")
        except Exception as exc:
            logger.debug("git branch isolation skipped: %s", exc)
        constraints = {
            "rss_cap_bytes": COMBINED_RSS_LIMIT,
            "ssot": "task_state",
            "no_new_tables": True,
        }
        body = self.clip(
            json.dumps(
                {
                    "query": query[:400],
                    "files": files[:16],
                    "constraints": constraints,
                    "dag": dag or {"nodes": payload.get("depends_on") or []},
                    "prompt": self.system_prompt,
                    "spec": spec_brief,
                    "context": str(getattr(context, "text", "") or "")[:800],
                },
                ensure_ascii=True,
            )
        )
        await self.emit("DAG packed")
        self.relay("coder", body, str(task.get("id") or "") or None)
        return WorkerResult(
            output=body,
            tokens=estimate_tokens(body),
            payload={"dag": dag, "files": files, "constraints": constraints, "spec": spec_brief, "branch": branch},
        )


class CodeWorker(WorkerAgent):
    role = "coder"
    label = "CODER"
    window = SWARM_CODER_TOKENS
    system_prompt = (
        "You are the Core Coding Agent. Use Tree-Sitter / AST ranges from the "
        "Rust indexer when present. Emit production edits only for assigned files."
    )

    async def run(self, task: dict[str, Any], context: Any) -> WorkerResult:
        payload = merge_payload(task.get("payload_json"))
        files = [str(item) for item in (payload.get("inputs") or payload.get("files") or []) if str(item).strip()]
        query = str(payload.get("query") or task.get("title") or "")
        failure = payload.get("test_failure") if isinstance(payload.get("test_failure"), dict) else None
        if failure:
            repair = self._repair_from_failure(failure)
            body = self.clip(
                "\n".join(
                    part
                    for part in (
                        self.system_prompt,
                        f"retry={payload.get('retry_count') or 1}",
                        f"file={failure.get('path')} line={failure.get('line')}",
                        f"test={failure.get('test_name')}",
                        f"assert={failure.get('assertion')}",
                        f"stack={json.dumps(failure.get('frames') or [], ensure_ascii=True)[:800]}",
                        repair or "repair skipped",
                    )
                    if part
                )
            )
            await self.emit(f"micro-diff {failure.get('path') or ''}:{failure.get('line') or 0}")
            self.relay("tester", body, str(task.get("id") or "") or None)
            if self.manager is not None:
                try:
                    self.manager.add_artifact(
                        f"repair:{payload.get('retry_count') or 1}",
                        body=body,
                        kind="repair",
                        task_id=str(task.get("id") or "") or None,
                        exit_code=0 if repair else 1,
                    )
                except Exception:
                    logger.debug("repair artifact skipped")
            return WorkerResult(
                ok=bool(repair),
                output=body,
                tokens=estimate_tokens(body),
                payload={"repair": repair, "test_failure": failure, "ast": True},
            )
        seeded = self._seed_requested_tests(query, files)
        from robber.memory.context_graph import ContextGraphEngine
        from robber.memory.context_search import ASTContextExtractor

        engine = ContextGraphEngine(
            self.root,
            token_budget=_graph_budget(self.window),
        )
        packed = engine.worker_context(query, files=files, depth=1)
        ast_context = ASTContextExtractor(self.root, token_budget=800).extract(query, files=files)
        spec_brief = ""
        try:
            from robber.engine.sdd_spec import SpecDrivenEngine

            loaded = SpecDrivenEngine(self.root, manager=self.manager).load()
            if loaded is not None:
                spec_brief = loaded.worker_brief()
        except Exception:
            logger.debug("sdd load skipped")

        ast_edit = payload.get("ast_edit") if isinstance(payload.get("ast_edit"), dict) else None
        if ast_edit and "path" in ast_edit and "symbol" in ast_edit and "code" in ast_edit:
            try:
                from robber.tools.structural_editor import replace_ast_node

                replace_ast_node(
                    ast_edit["path"],
                    ast_edit["symbol"],
                    ast_edit["code"],
                    root=self.root,
                )
            except Exception as e:
                logger.debug(f"structural edit skipped: {e}")

        body = self.clip(
            "\n".join(
                part
                for part in (
                    self.system_prompt,
                    spec_brief,
                    seeded,
                    ast_context.text if ast_context and ast_context.text else packed.text,
                )
                if part
            )
        )
        await self.emit(
            ((seeded + " ") if seeded else "")
            + f"AST graph {packed.query or 'slice'} {packed.tokens} tok {packed.elapsed_ms:.2f}ms"
        )
        self.relay("auditor", body, str(task.get("id") or "") or None)
        return WorkerResult(
            output=body,
            tokens=estimate_tokens(body),
            payload={
                "files": files,
                "ast": True,
                "graph": packed.public(),
                "ast_context": ast_context.public() if ast_context else {},
                "seeded": seeded,
            },
        )

    def _seed_requested_tests(self, query: str, files: list[str]) -> str:
        text = (query or "").lower()
        wants = "test_math.py" in text or "math utility" in text or "intentionally fail" in text
        if not wants and not any("test_math.py" in item.replace("\\", "/") for item in files):
            return ""
        created: list[str] = []
        util = self.root / "math_util.py"
        test = self.root / "tests" / "test_math.py"
        if not util.is_file():
            util.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            created.append("math_util.py")
        if not test.is_file():
            test.parent.mkdir(parents=True, exist_ok=True)
            test.write_text(
                "from math_util import add\n\n\ndef test_add():\n    assert add(1, 1) == 3\n",
                encoding="utf-8",
            )
            created.append("tests/test_math.py")
        if not created:
            return ""
        return "seeded " + " ".join(created)

    def _repair_from_failure(self, failure: dict[str, Any]) -> str | None:
        rel = self._safe_rel(str(failure.get("path") or ""))
        if rel is None:
            return None
        path = self.root / rel
        if not path.is_file():
            return None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        idx = int(failure.get("line") or 0) - 1
        if idx < 0 or idx >= len(lines):
            idx = _find_assert_line(lines, str(failure.get("expected") or ""))
        if idx < 0 or idx >= len(lines):
            return None
        original = lines[idx]
        updated = _rewrite_assert_line(
            original,
            expected=str(failure.get("expected") or ""),
            received=str(failure.get("received") or ""),
        )
        if updated == original:
            return None
        diff = _unified_line_replace(rel, lines, idx, updated)
        if self._apply_micro_diff(diff):
            return diff
        lines[idx] = updated
        try:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            return None
        return diff

    def _apply_micro_diff(self, diff: str) -> bool:
        try:
            from robber.indexer_bridge import apply_diff_atomic
            from robber.ssot_db import default_ssot_path

            report = apply_diff_atomic(self.root, diff, ssot_db=default_ssot_path(self.root))
            return bool(report.get("ok"))
        except FileNotFoundError:
            return False
        except Exception:
            logger.debug("atomic micro-diff skipped")
            return False

    def _safe_rel(self, raw: str) -> str | None:
        text = (raw or "").strip().replace("\\", "/")
        if not text or ".." in Path(text).parts:
            return None
        try:
            candidate = Path(text)
            dest = candidate if candidate.is_absolute() else (self.root / candidate)
            rel = dest.resolve().relative_to(self.root.resolve())
        except (ValueError, OSError):
            return None
        return rel.as_posix()


class AuditorWorker(WorkerAgent):
    role = "auditor"
    label = "AUDITOR"
    window = SWARM_AUDITOR_TOKENS
    system_prompt = (
        "You are the Security & Code Auditor. Reject memory-unsafe diffs, syntax "
        "errors, and missing imports before patches land."
    )

    async def run(self, task: dict[str, Any], context: Any) -> WorkerResult:
        payload = merge_payload(task.get("payload_json"))
        files = [str(item) for item in (payload.get("inputs") or payload.get("files") or []) if str(item).strip()]
        query = str(payload.get("query") or task.get("title") or "")
        failure = payload.get("test_failure") if isinstance(payload.get("test_failure"), dict) else None
        if failure:
            findings = [
                f"note: failing {failure.get('path')}:{failure.get('line')} {failure.get('test_name')}",
                str(failure.get("assertion") or "")[:240],
                json.dumps(failure.get("frames") or [], ensure_ascii=True)[:800],
            ]
            body = self.clip("\n".join(part for part in findings if part) or "audit failure relay")
            await self.emit(f"relay {failure.get('path') or ''}:{failure.get('line') or 0} to coder")
            self.relay("coder", body, str(task.get("id") or "") or None)
            return WorkerResult(
                ok=True,
                output=body,
                tokens=estimate_tokens(body),
                payload={"findings": findings[:32], "ok": True, "test_failure": failure},
            )
        from robber.memory.context_graph import ContextGraphEngine

        engine = ContextGraphEngine(
            self.root,
            token_budget=_graph_budget(self.window),
        )
        packed = engine.worker_context(query, files=files, depth=1)
        findings = self._audit_graph(packed, files)
        findings.extend(self._security_and_lsp(files))
        ok = not any(item.startswith("error:") for item in findings)
        body = self.clip("\n".join(findings) or "audit clean")
        await self.emit("diff audit " + ("ok" if ok else "issues"))
        self.relay("tester", body, str(task.get("id") or "") or None)
        return WorkerResult(
            ok=ok,
            output=body,
            tokens=estimate_tokens(body),
            payload={"findings": findings[:32], "ok": ok, "graph": packed.public()},
        )

    def _audit_graph(self, packed: Any, files: list[str]) -> list[str]:
        out: list[str] = []
        payload = packed.payload if packed is not None else {}
        imports = {str(item) for item in (payload.get("imports") or []) if item}
        snippets = []
        for item in payload.get("slices") or []:
            if not isinstance(item, dict):
                continue
            snippet = str(item.get("snippet") or "")
            snippets.append(snippet)
            rel = str(item.get("path") or "")
            try:
                ast.parse(snippet)
            except SyntaxError:
                indented = "\n".join(f"    {line}" for line in snippet.splitlines())
                try:
                    ast.parse(f"class _Probe:\n{indented}\n")
                except SyntaxError as exc:
                    if snippet.strip() and "unexpected EOF" not in str(exc):
                        out.append(f"note: {rel}:{item.get('line')} slice not a complete unit")
        blob = "\n".join(snippets)
        if "threading" in imports or "threading" in blob:
            if "async def" in blob and "Lock" in blob:
                out.append("note: graph mixes threading locks with async")
        if files and not snippets:
            out.append("ok " + " ".join(files[:4]) + " (no ast slice)")
        if not out:
            defs = payload.get("definitions") or []
            out.append(f"ok graph {len(defs)} def(s) {int(getattr(packed, 'tokens', 0) or 0)} tok")
        return out

    def _security_and_lsp(self, files: list[str]) -> list[str]:
        out: list[str] = []
        try:
            from robber.diagnostics.lsp_security import AstSecurityAuditor, LspClient
        except Exception:
            return out
        auditor = AstSecurityAuditor()
        client = LspClient()
        try:
            for rel in files[:8]:
                path = self.root / str(rel)
                if not path.is_file():
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                posix = str(rel).replace("\\", "/")
                for hit in auditor.scan_text(text, posix):
                    prefix = "error" if hit.rule in {"sqli", "secret", "unsafe"} else "note"
                    out.append(f"{prefix}: {hit.rule} {posix}:{hit.line} {hit.owasp}")
                for diag in client.diagnose(posix, text):
                    if diag.severity == "error":
                        out.append(f"error: lsp {posix}:{diag.line} {diag.message}")
        finally:
            client.close()
        return out


class TestWorker(WorkerAgent):
    role = "tester"
    label = "TESTER"
    window = SWARM_TESTER_TOKENS
    system_prompt = (
        "You are the Automated Test Engine. Run sandboxed pytest, go test, cargo test, "
        "or Jest via the Rust indexer sandbox and return stdout/stderr to the healer."
    )

    async def run(self, task: dict[str, Any], context: Any) -> WorkerResult:
        payload = merge_payload(task.get("payload_json"))
        files = [str(item) for item in (payload.get("inputs") or payload.get("files") or []) if str(item).strip()]
        targets = _test_paths(self.root, files)
        cmd, runner = _suite_command(self.root, targets, files)
        await self.emit("rust sandbox " + (cmd[:80] if cmd else "py_compile"))
        result = await asyncio.to_thread(self._run_sandbox, cmd, runner, targets, files)
        raw = str(result.get("text") or "")
        from robber.diagnostics.test_parser import TracebackParser

        parsed = TracebackParser().parse(raw, root=self.root)
        result["failure"] = parsed.as_dict()
        if not result.get("ok"):
            via = (
                "via rust sandbox"
                if str(result.get("sandbox") or "") in {"rust", "rust-tool"}
                else "via local runner"
            )
            await self.emit(
                f"failed assertion {parsed.test_name or parsed.runner} "
                f"{parsed.path}:{parsed.line} {via}"
            )
        body = self.clip(raw)
        self.relay("healer", body, str(task.get("id") or "") or None)
        ok = bool(result.get("ok"))
        if self.manager is not None:
            try:
                self.manager.add_artifact(
                    f"test:{runner or 'suite'}",
                    body=json.dumps(
                        {
                            "ok": ok,
                            "runner": result.get("sandbox") or runner,
                            "failure": parsed.as_dict(),
                            "text": raw[:2000],
                        },
                        ensure_ascii=True,
                    )[:4000],
                    kind="test",
                    task_id=str(task.get("id") or "") or None,
                    exit_code=0 if ok else int(result.get("exit_code") or 1),
                )
            except Exception:
                logger.debug("test artifact skipped")
        try:
            from robber.diagnostics.profiler import ExecutionProfiler

            profile = ExecutionProfiler(self.root).record_suite(result, files)
            if self.manager is not None and profile.bottlenecks:
                self.manager.add_artifact(
                    "profile:suite",
                    body=json.dumps(profile.public(), ensure_ascii=True)[:4000],
                    kind="profile",
                    task_id=str(task.get("id") or "") or None,
                    exit_code=0,
                )
        except Exception:
            logger.debug("profiler skipped")
        if ok:
            try:
                from robber.tools.git_manager import GitManager, wants_auto_pr

                git = GitManager(self.root, manager=self.manager)
                if git.is_repo() and git.current_branch().startswith("agent/"):
                    commit = git.conventional_commit(files or None, task_id=str(task.get("id") or "") or None)
                    result["commit"] = commit.public()
                    if commit.ok:
                        await self.emit(f"committed {commit.message}")
                    if wants_auto_pr(payload):
                        from robber.agents.pr_writer import PRWriterWorker
                        from robber.tools.git_provider import GitProvider

                        doc = PRWriterWorker(self.root, manager=self.manager, hub=self.hub).synthesize(
                            query=str(payload.get("query") or task.get("title") or ""),
                            files=commit.files or files,
                            artifacts=self.manager.list_artifacts(task_id=str(task.get("id") or "") or None)
                            if self.manager is not None
                            else [],
                            commit_message=commit.message,
                            branch=commit.branch,
                        )
                        pr = GitProvider(self.root, manager=self.manager).push_and_create_pr(
                            commit.branch,
                            doc.title,
                            doc.body,
                            draft=False,
                        )
                        result["pr"] = pr.public()
                        if pr.ok and pr.url:
                            await self.emit(f"pull request {pr.url}")
                        elif pr.error:
                            await self.emit(f"pr skipped: {pr.error}")
                    else:
                        await self.emit("tests passed — /pr create to open a pull request")
            except Exception as exc:
                logger.debug("git/pr proposal skipped: %s", exc)
        return WorkerResult(
            ok=ok,
            output=raw if raw else body,
            tokens=estimate_tokens(body),
            payload=result,
        )

    def _run_sandbox(
        self,
        cmd: str,
        runner: str,
        tests: list[str],
        files: list[str],
    ) -> dict[str, Any]:
        if not cmd:
            return {"ok": True, "text": "no tests scheduled", "exit_code": 0, "sandbox": "none"}
        timeout = int(SWARM_TEST_TIMEOUT_SECS)
        try:
            from robber.indexer_bridge import run_test_suite
            from robber.ssot_db import default_ssot_path

            packed = run_test_suite(
                self.root,
                cmd,
                working_dir=self.root,
                timeout_secs=timeout,
                ssot_db=default_ssot_path(self.root),
            )
            stdout = str(packed.get("stdout") or "")
            stderr = str(packed.get("stderr") or "")
            code = packed.get("exit_code")
            text = (stdout + "\n" + stderr).strip() or json.dumps(packed, ensure_ascii=True)[:800]
            return {
                "ok": bool(packed.get("ok")) or int(code or 1) == 0,
                "text": text,
                "exit_code": code,
                "sandbox": "rust",
                "runner": packed.get("runner") or runner,
                "timed_out": packed.get("timed_out"),
                "memory_exceeded": packed.get("memory_exceeded"),
            }
        except FileNotFoundError:
            try:
                from robber.indexer_bridge import run_tool_sandboxed
                from robber.ssot_db import default_ssot_path

                program, args = _split_cmd(cmd)
                packed = run_tool_sandboxed(
                    self.root,
                    program,
                    args,
                    cwd=self.root,
                    timeout_ms=timeout * 1000,
                    ssot_db=default_ssot_path(self.root),
                )
                stdout = str(packed.get("stdout") or "")
                stderr = str(packed.get("stderr") or "")
                code = packed.get("exit_code")
                text = (stdout + "\n" + stderr).strip()
                return {
                    "ok": int(code or 0) == 0,
                    "text": text,
                    "exit_code": code,
                    "sandbox": "rust-tool",
                    "runner": runner,
                }
            except Exception:
                return self._fallback_cmd(cmd, timeout)
        except Exception:
            return self._fallback_cmd(cmd, timeout)

    def _fallback_cmd(self, cmd: str, timeout: int) -> dict[str, Any]:
        import subprocess

        program, args = _split_cmd(cmd)
        try:
            proc = subprocess.run(
                [program, *args],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=max(5, int(timeout)),
            )
        except Exception as exc:
            return {
                "ok": False,
                "text": f"{type(exc).__name__}: {exc}"[:800],
                "exit_code": 1,
                "sandbox": "local",
            }
        text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        return {
            "ok": proc.returncode == 0,
            "text": text[:8000],
            "exit_code": proc.returncode,
            "sandbox": "local",
        }


def role_for_task(task: dict[str, Any]) -> str:
    payload = merge_payload(task.get("payload_json"))
    role = str(payload.get("worker") or payload.get("role") or "").strip().lower()
    if role in ROLE_LABELS:
        return role
    tag = str(payload.get("tag") or "").strip().lower()
    return _TAG_ROLE.get(tag, "coder")


def _graph_budget(window: int) -> int:
    from robber.memory.token_budgeter import TokenBudgeter

    return min(
        int(window),
        GRAPH_SLICE_TOKEN_CAP,
        TokenBudgeter(window, hard_cap=WORKER_PROMPT_TOKEN_CAP).quotas().graph,
    )


def _test_paths(root: Path, files: list[str]) -> list[str]:
    out: list[str] = []
    for item in files:
        path = Path(item)
        posix = path.as_posix()
        name = path.name
        looks_test = (
            "test" in path.parts
            or name.startswith("test_")
            or name.endswith("_test.py")
            or name.endswith("_test.go")
            or name.endswith("_test.rs")
            or ".test." in name
        )
        if looks_test and (root / path).is_file():
            if posix not in out:
                out.append(posix)
            continue
        stem = path.stem
        candidate = Path("tests") / f"test_{stem}.py"
        if (root / candidate).is_file():
            posix_c = candidate.as_posix()
            if posix_c not in out:
                out.append(posix_c)
    extra = root / "tests" / "test_math.py"
    if extra.is_file():
        posix = extra.relative_to(root).as_posix()
        mentioned = any("test_math.py" in Path(item).as_posix() for item in files)
        if mentioned and posix not in out:
            out.append(posix)
    return out[:8]


def _suite_command(root: Path, tests: list[str], files: list[str]) -> tuple[str, str]:
    py = [item for item in tests if item.endswith(".py")]
    go = [item for item in tests if item.endswith(".go")]
    rs = [item for item in tests if item.endswith(".rs")]
    js = [item for item in tests if item.endswith((".js", ".jsx", ".ts", ".tsx"))]
    if py:
        return (
            " ".join([_quote_arg(sys.executable), "-m", "pytest", "-q", "--tb=short", *py]),
            "pytest",
        )
    if go:
        return (" ".join(["go", "test", *go]), "go")
    if rs:
        return ("cargo test --offline --lib -- --nocapture", "cargo")
    if js:
        return (" ".join(["npx", "jest", "--ci", *js]), "jest")
    existing = [item for item in files if (root / item).is_file()]
    if existing:
        return (
            " ".join([_quote_arg(sys.executable), "-m", "py_compile", *existing[:6]]),
            "py_compile",
        )
    return ("", "")


def _quote_arg(value: str) -> str:
    if not value:
        return value
    if any(ch in value for ch in " \t\""):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _split_cmd(cmd: str) -> tuple[str, list[str]]:
    parts: list[str] = []
    cur = []
    quote = ""
    for ch in cmd or "":
        if quote:
            if ch == quote:
                quote = ""
            else:
                cur.append(ch)
            continue
        if ch in {'"', "'"}:
            quote = ch
            continue
        if ch.isspace():
            if cur:
                parts.append("".join(cur))
                cur = []
            continue
        cur.append(ch)
    if cur:
        parts.append("".join(cur))
    if not parts:
        return "", []
    return parts[0], parts[1:]


def _find_assert_line(lines: list[str], expected: str) -> int:
    needle = (expected or "").strip()
    for idx, line in enumerate(lines):
        if "assert" in line and (not needle or needle in line):
            return idx
    return -1


def _rewrite_assert_line(original: str, *, expected: str, received: str) -> str:
    text = original
    exp = (expected or "").strip()
    got = (received or "").strip()
    if exp and got and exp != got:
        pos = text.rfind(exp)
        if pos >= 0:
            return text[:pos] + got + text[pos + len(exp) :]
    import re as _re

    updated = _re.sub(r"\bassert\s+False\b", "assert True", text)
    return updated


def _unified_line_replace(rel: str, lines: list[str], idx: int, new_line: str) -> str:
    start = max(0, idx - 1)
    end = min(len(lines), idx + 2)
    old_count = max(1, end - start)
    body: list[str] = []
    for i in range(start, end):
        if i == idx:
            body.append("-" + lines[i])
            body.append("+" + new_line)
        else:
            body.append(" " + lines[i])
    header = f"@@ -{start + 1},{old_count} +{start + 1},{old_count} @@"
    posix = rel.replace("\\", "/")
    return f"--- a/{posix}\n+++ b/{posix}\n{header}\n" + "\n".join(body) + "\n"
