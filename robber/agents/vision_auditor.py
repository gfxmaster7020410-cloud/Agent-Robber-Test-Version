"""Multimodal visual UI auditor and frontend self-healing loop worker."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from robber.agents.sub_agents import WorkerAgent, WorkerResult
from robber.config import BROWSER_SNAPSHOT_DIR, WORKER_PROMPT_TOKEN_CAP
from robber.context_assembly import merge_payload
from robber.diagnostics.browser_inspector import BrowserInspectionReport, BrowserInspector
from robber.jail import JailError, display_path, resolve_inside, workspace_root
from robber.security.perimeter import validate_file_mutation
from robber.ssot_context import estimate_tokens
from robber.tools.browser_engine import BrowserSnapshot, PlaywrightBrowserEngine
from robber.tools.structural_editor import EditError, replace_ast_node

logger = logging.getLogger(__name__)

SWARM_VISION_TOKENS = 2048


@dataclass
class VisionAuditResult:
    url: str
    ok: bool
    summary: str
    issues_found: list[str] = field(default_factory=list)
    repaired: bool = False
    repair_details: dict[str, Any] = field(default_factory=dict)
    screenshot_before: str = ""
    screenshot_after: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class VisionAuditorWorker(WorkerAgent):
    """Multimodal UI auditor correlating screenshots, DOM trees, and console tracebacks."""

    role = "vision_auditor"
    label = "VISION_AUDITOR"
    window = SWARM_VISION_TOKENS
    system_prompt = (
        "You are the Visual UI Auditor and Frontend Healing Engine. Analyze browser "
        "screenshots, DOM accessibility hierarchies, and JavaScript console/network "
        "errors to diagnose rendering flaws and construct precision AST fixes."
    )

    async def run(self, task: dict[str, Any], context: Any) -> WorkerResult:
        payload = merge_payload(task.get("payload_json"))
        url = str(payload.get("url") or payload.get("target") or "http://127.0.0.1:3000")
        auto_heal = bool(payload.get("auto_heal", True))

        await self.emit(f"capturing web snapshot for {url}")

        engine = PlaywrightBrowserEngine(self.root)
        inspector = BrowserInspector(self.root)

        try:
            snapshot = await engine.navigate_and_capture(url)
            report = inspector.inspect_snapshot(snapshot)

            issues: list[str] = []
            if not snapshot.ok:
                issues.append(f"Page returned status {snapshot.status_code}")
            for err in report.page_errors:
                issues.append(f"Page Error: {err.get('message')}")
            for err in report.console_errors:
                issues.append(f"Console {err.get('type')}: {err.get('text')}")
            for req in report.failed_requests:
                issues.append(f"Failed Request: {req.get('method')} {req.get('url')}")

            repaired = False
            repair_meta = {}
            post_snapshot_path = snapshot.screenshot_path

            if issues and auto_heal:
                await self.emit(f"found {len(issues)} frontend issue(s) — attempting visual AST repair")
                repair_meta = await self.heal_frontend_issue(url, report, engine)
                if repair_meta.get("ok"):
                    repaired = True
                    post_snapshot_path = repair_meta.get("post_screenshot", snapshot.screenshot_path)
                    await self.emit("re-verified browser session: 0 console errors, clean render")
                else:
                    await self.emit(f"repair skipped or inconclusive: {repair_meta.get('reason', '')}")

            summary = (
                f"Visual UI Audit for {url}: "
                + (f"Healed {len(issues)} issue(s)" if repaired else ("Clean" if not issues else f"{len(issues)} issue(s) found"))
            )

            result_obj = VisionAuditResult(
                url=url,
                ok=(not issues) or repaired,
                summary=summary,
                issues_found=issues,
                repaired=repaired,
                repair_details=repair_meta,
                screenshot_before=snapshot.screenshot_path,
                screenshot_after=post_snapshot_path,
            )

            body = json.dumps(result_obj.as_dict(), indent=2)

            if self.manager is not None:
                try:
                    self.manager.add_artifact(
                        "visual_audit",
                        body=body,
                        kind="vision",
                        task_id=str(task.get("id") or "") or None,
                        exit_code=0 if result_obj.ok else 1,
                    )
                except Exception:
                    pass

            return WorkerResult(
                ok=result_obj.ok,
                output=body,
                tokens=estimate_tokens(body),
                payload=result_obj.as_dict(),
            )

        finally:
            await engine.close()

    async def heal_frontend_issue(
        self,
        url: str,
        report: BrowserInspectionReport,
        engine: PlaywrightBrowserEngine,
    ) -> dict[str, Any]:
        """Perform autonomous AST-node correction on buggy frontend code and re-verify page."""
        target = report.dom_target
        if not target.file_path or not target.line:
            return {"ok": False, "reason": "could not pinpoint source target from traceback"}

        src_file = self.root / target.file_path
        if not src_file.is_file():
            return {"ok": False, "reason": f"target source file not found: {target.file_path}"}

        # Check self-codebase protection
        try:
            validate_file_mutation(str(src_file))
        except Exception as exc:
            return {"ok": False, "reason": f"self-protection blocked: {exc}"}

        original_code = src_file.read_text(encoding="utf-8")

        # Generate a candidate fix for common JS/TS runtime crashes
        fixed_code = self._synthesize_fix(original_code, report)
        if not fixed_code or fixed_code == original_code:
            return {"ok": False, "reason": "no syntax repair synthesized"}

        try:
            rel = display_path(src_file, self.root)
            if target.component and target.component != "DOMRoot":
                try:
                    replace_ast_node(
                        rel,
                        target.component,
                        fixed_code,
                        root=self.root,
                    )
                except Exception:
                    src_file.write_text(fixed_code, encoding="utf-8")
            else:
                src_file.write_text(fixed_code, encoding="utf-8")

            # Re-verify page in Playwright
            retest_snap = await engine.navigate_and_capture(url)
            new_inspector = BrowserInspector(self.root)
            new_report = new_inspector.inspect_snapshot(retest_snap)

            if not new_report.has_errors:
                return {
                    "ok": True,
                    "target_file": rel,
                    "post_screenshot": retest_snap.screenshot_path,
                    "status_code": retest_snap.status_code,
                }
            else:
                # Rollback if not fixed
                src_file.write_text(original_code, encoding="utf-8")
                return {
                    "ok": False,
                    "reason": f"re-test still has {len(new_report.page_errors) + len(new_report.console_errors)} error(s)",
                }

        except Exception as exc:
            src_file.write_text(original_code, encoding="utf-8")
            return {"ok": False, "reason": str(exc)}

    def _synthesize_fix(self, source: str, report: BrowserInspectionReport) -> str:
        """Rule-based and heuristic AST repair for common client-side crashes."""
        err_msg = ""
        for p in report.page_errors:
            err_msg += str(p.get("message", "")) + "\n"
        for c in report.console_errors:
            err_msg += str(c.get("text", "")) + "\n"

        target = report.dom_target
        lines = source.splitlines()

        if 0 < target.line <= len(lines):
            target_idx = target.line - 1
            bad_line = lines[target_idx]

            # Fix 1: Cannot read properties of undefined (reading 'x') -> add optional chaining
            null_prop_m = re.search(
                r"Cannot\s+read\s+propert(?:y|ies)\s+of\s+(?:undefined|null)\s+\(reading\s+['\"](?P<prop>\w+)['\"]\)",
                err_msg,
                re.I,
            )
            if null_prop_m:
                prop = null_prop_m.group("prop")
                if f".{prop}" in bad_line:
                    lines[target_idx] = bad_line.replace(f".{prop}", f"?.{prop}")
                    return "\n".join(lines) + "\n"

            # Fix 2: is not a function -> add optional invocation or fallback
            not_fn_m = re.search(r"(?P<fn>\w+)\s+is\s+not\s+a\s+function", err_msg, re.I)
            if not_fn_m:
                fn = not_fn_m.group("fn")
                if f"{fn}(" in bad_line:
                    lines[target_idx] = bad_line.replace(f"{fn}(", f"({fn} && {fn})(")
                    return "\n".join(lines) + "\n"

            # Fix 3: is not defined -> declare safe fallback
            not_def_m = re.search(r"(?P<var>\w+)\s+is\s+not\s+defined", err_msg, re.I)
            if not_def_m:
                var_name = not_def_m.group("var")
                # Prepend safe declaration if missing
                return f"// safe fallback definition\nconst {var_name} = typeof {var_name} !== 'undefined' ? {var_name} : {{}};\n" + source

        return source
