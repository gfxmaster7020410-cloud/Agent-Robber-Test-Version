"""Intelligent multi-tier security policy engine for shell commands."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robber.jail import workspace_root
from robber.security.perimeter import is_protected_path

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "command_rules.json"

TIER_1_AUTO_APPROVE = 1
TIER_2_USER_CONFIRMATION = 2
TIER_3_BLOCKED = 3

TIER_NAMES = {
    TIER_1_AUTO_APPROVE: "TIER_1_AUTO_APPROVE",
    TIER_2_USER_CONFIRMATION: "TIER_2_USER_CONFIRMATION",
    TIER_3_BLOCKED: "TIER_3_BLOCKED",
}

DEFAULT_TIER1 = [
    "git status", "git diff", "git log", "git show", "git rev-parse", "git branch",
    "pwd", "ls", "dir", "echo", "printf", "type", "cat", "head", "tail",
    "grep", "find", "rg", "where", "which", "whoami",
    "python --version", "python -V", "py --version", "node --version", "npm -v",
    "npm test", "npm run test", "pytest", "go test", "go version",
    "cargo check", "cargo test", "cargo --version"
]

DEFAULT_TIER2 = [
    "pip install", "pip uninstall", "python -m pip", "npm install", "npm i ",
    "npm ci", "npm uninstall", "npm start", "npm run", "cargo add", "cargo install",
    "git commit", "git push", "git checkout", "git reset", "git rebase",
    "git merge", "git stash", "git clean", "curl", "wget"
]

DEFAULT_TIER3 = [
    "sudo", "runas", "doas", "pkexec", "gsudo",
    "rm -rf /", "rmdir /s /q", "format ", "mkfs", "diskpart"
]

_SPLIT_OPERATORS = re.compile(r"(\&\&|\|\||;|\||\n)")
_REDIRECT_OP = re.compile(r"(>>|>\s*|tee\s+-a|tee\s+|out-file|set-content|add-content)", re.I)
_BLOCKED_PATTERNS = [
    (re.compile(r"\brm\s+-[rf]{1,2}\s+(--no-preserve-root\s+)?[/\\](\s|$)", re.I), "refusing recursive delete of filesystem root"),
    (re.compile(r"\brm\s+-[rf]{1,2}\s+[/\\]\*", re.I), "refusing recursive delete of filesystem root"),
    (re.compile(r"\bformat\s+[a-z]:", re.I), "refusing disk format"),
    (re.compile(r"\bformat-volume\b", re.I), "refusing disk format"),
    (re.compile(r"\bmkfs(\.\w+)?\b", re.I), "refusing mkfs"),
    (re.compile(r"\bdd\s+.*\bof\s*=\s*[/\\]dev[/\\]", re.I), "refusing raw disk write"),
    (re.compile(r"\bdiskpart\b", re.I), "refusing diskpart"),
    (re.compile(r"\bdel\s+/[fsq]+\s+[c-z]:\\?\s*$", re.I), "refusing wipe of a drive root"),
    (re.compile(r"\bshutdown\b|\breboot\b|\binit\s+0\b|\bpoweroff\b", re.I), "refusing shutdown/reboot"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;"), "refusing fork bomb"),
    (re.compile(r"\bwhile\s+(true|:|1)\b", re.I), "refusing unbounded loop"),
    (re.compile(r"\bfor\s*\(\s*;\s*;\s*\)", re.I), "refusing unbounded loop"),
    (re.compile(r"\b(sudo|runas|doas|pkexec|gsudo)\b", re.I), "refusing privilege escalation"),
]


@dataclass
class PolicyEvaluation:
    tier: int
    tier_name: str
    command: str
    reason: str
    subcommands: list[str] = field(default_factory=list)
    blocked: bool = False
    requires_confirmation: bool = False

    @property
    def allowed(self) -> bool:
        return not self.blocked


class CommandPolicyEngine:
    """Classifies shell commands into Security Tiers and handles interactive approval."""

    def __init__(self, rules_path: Path | str | None = None, *, workspace: Path | None = None) -> None:
        self.workspace = (workspace or workspace_root()).resolve()
        self.rules_path = Path(rules_path) if rules_path else CONFIG_PATH
        self.tier1_rules: list[str] = list(DEFAULT_TIER1)
        self.tier2_rules: list[str] = list(DEFAULT_TIER2)
        self.tier3_rules: list[str] = list(DEFAULT_TIER3)
        self.trust_prefixes: list[str] = []
        self._pending: dict[str, dict[str, Any]] = {}
        self.load_rules()

    def load_rules(self) -> None:
        if not self.rules_path.is_file():
            return
        try:
            data = json.loads(self.rules_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if isinstance(data.get("tier1"), list):
                    self.tier1_rules = [str(x) for x in data["tier1"]]
                if isinstance(data.get("tier2"), list):
                    self.tier2_rules = [str(x) for x in data["tier2"]]
                if isinstance(data.get("tier3"), list):
                    self.tier3_rules = [str(x) for x in data["tier3"]]
                if isinstance(data.get("trust_prefixes"), list):
                    self.trust_prefixes = [str(x) for x in data["trust_prefixes"]]
        except Exception as exc:
            logger.debug(f"Failed to load command rules: {exc}")

    def trust_prefix(self, prefix: str) -> None:
        p = prefix.strip()
        if p and p not in self.trust_prefixes:
            self.trust_prefixes.append(p)

    def reset_trust(self) -> None:
        self.trust_prefixes.clear()

    def tokenize_subcommands(self, command: str) -> list[str]:
        raw = (command or "").strip()
        if not raw:
            return []
        parts: list[str] = []
        current: list[str] = []
        in_single = False
        in_double = False
        i = 0
        n = len(raw)

        while i < n:
            c = raw[i]
            if c == "'" and not in_double:
                in_single = not in_single
                current.append(c)
                i += 1
            elif c == '"' and not in_single:
                in_double = not in_double
                current.append(c)
                i += 1
            elif not in_single and not in_double:
                if raw[i:i+2] in ("&&", "||"):
                    sub = "".join(current).strip()
                    if sub:
                        parts.append(sub)
                    current.clear()
                    i += 2
                elif c in (";", "|", "\n", "&"):
                    sub = "".join(current).strip()
                    if sub:
                        parts.append(sub)
                    current.clear()
                    i += 1
                else:
                    current.append(c)
                    i += 1
            else:
                current.append(c)
                i += 1

        sub = "".join(current).strip()
        if sub:
            parts.append(sub)

        return parts if parts else [raw]

    def evaluate_subcommand(self, subcmd: str, workspace: Path) -> tuple[int, str]:
        cmd_clean = subcmd.strip()
        if not cmd_clean:
            return TIER_1_AUTO_APPROVE, "empty"

        # Check dynamic trust prefixes first
        for prefix in self.trust_prefixes:
            if cmd_clean.startswith(prefix) or cmd_clean == prefix:
                return TIER_1_AUTO_APPROVE, f"trusted prefix: {prefix}"

        # 1. Check Tier 3 (Blocked / System Threat)
        for pat, reason in _BLOCKED_PATTERNS:
            if pat.search(cmd_clean):
                return TIER_3_BLOCKED, reason

        for rule in self.tier3_rules:
            if rule.lower() in cmd_clean.lower() or cmd_clean.lower().startswith(rule.lower()):
                return TIER_3_BLOCKED, f"matches blocked rule: {rule}"

        # Check redirection or writes to protected agent files
        if _REDIRECT_OP.search(cmd_clean):
            # Extract tokens
            for token in cmd_clean.split():
                token_clean = token.strip().strip("'\"")
                if token_clean in (">", ">>", "|", "tee"):
                    continue
                try:
                    cand = Path(token_clean)
                    resolved = cand.resolve() if cand.is_absolute() else (workspace / cand).resolve()
                    if is_protected_path(resolved):
                        return TIER_3_BLOCKED, f"refusing to mutate protected agent file: {token_clean}"
                except OSError:
                    pass

        # 2. Check Tier 1 (Auto-approve / Read-only / Safe)
        cmd_lower = cmd_clean.lower()
        for rule in self.tier1_rules:
            r_lower = rule.lower()
            if cmd_lower == r_lower or cmd_lower.startswith(r_lower + " ") or cmd_lower.startswith(r_lower + "-"):
                return TIER_1_AUTO_APPROVE, f"matches safe rule: {rule}"

        # 3. Check Tier 2 (Confirmation Required / Mutating Operations)
        for rule in self.tier2_rules:
            r_lower = rule.lower()
            if cmd_lower == r_lower or cmd_lower.startswith(r_lower + " ") or r_lower in cmd_lower:
                return TIER_2_USER_CONFIRMATION, f"mutating operation: {rule}"

        # Default: if command has flags or appears to modify state, Tier 2
        if any(op in cmd_clean for op in (">", ">>", "rm", "del", "mkdir", "touch", "cp", "mv")):
            return TIER_2_USER_CONFIRMATION, "filesystem mutation"

        return TIER_2_USER_CONFIRMATION, "general execution requiring confirmation"

    def evaluate(self, command: str, workspace: Path | None = None) -> PolicyEvaluation:
        ws = workspace or self.workspace
        subcommands = self.tokenize_subcommands(command)
        if not subcommands:
            return PolicyEvaluation(
                tier=TIER_1_AUTO_APPROVE,
                tier_name=TIER_NAMES[TIER_1_AUTO_APPROVE],
                command=command,
                reason="empty command",
                subcommands=[],
            )

        highest_tier = TIER_1_AUTO_APPROVE
        highest_reason = "auto-approved"

        for sub in subcommands:
            tier, reason = self.evaluate_subcommand(sub, ws)
            if tier > highest_tier:
                highest_tier = tier
                highest_reason = reason
            if highest_tier == TIER_3_BLOCKED:
                break

        eval_res = PolicyEvaluation(
            tier=highest_tier,
            tier_name=TIER_NAMES.get(highest_tier, "UNKNOWN"),
            command=command,
            reason=highest_reason,
            subcommands=subcommands,
            blocked=(highest_tier == TIER_3_BLOCKED),
            requires_confirmation=(highest_tier == TIER_2_USER_CONFIRMATION),
        )

        if eval_res.blocked:
            self.record_audit(eval_res)

        return eval_res

    def record_audit(self, evaluation: PolicyEvaluation, project_id: str | None = None) -> None:
        try:
            from robber.ssot_db import SsotDB

            db = SsotDB()
            db.record_security_audit(
                command=evaluation.command,
                tier=evaluation.tier,
                reason=evaluation.reason,
                project_id=project_id,
            )
        except Exception as exc:
            logger.debug(f"Failed to record security audit log: {exc}")

    async def request_confirmation(
        self,
        command: str,
        tier: int = TIER_2_USER_CONFIRMATION,
        reason: str = "",
        timeout: float = 60.0,
    ) -> bool:
        # Check auto-approve env overrides
        if os.environ.get("ROBBER_AUTO_APPROVE_ALL") in {"1", "true", "yes"}:
            return True

        req_id = f"cmd_{uuid.uuid4().hex[:8]}"
        loop = asyncio.get_running_loop()
        event = asyncio.Event()

        item = {
            "id": req_id,
            "command": command,
            "tier": tier,
            "reason": reason or "Mutating shell operation",
            "event": event,
            "approved": False,
            "timestamp": time.time(),
        }
        self._pending[req_id] = item

        # Broadcast WebSocket notification
        frame = {
            "type": "COMMAND_PENDING_APPROVAL",
            "event": "COMMAND_CONFIRMATION_REQUEST",
            "id": req_id,
            "cmd": command,
            "tier": tier,
            "reason": reason,
        }
        try:
            from server import hub  # type: ignore

            if hub is not None:
                await hub.broadcast(frame)
        except Exception:
            pass

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return bool(item.get("approved"))
        except asyncio.TimeoutError:
            return False
        finally:
            self._pending.pop(req_id, None)

    def approve(self, request_id: str | None = None) -> bool:
        if request_id:
            item = self._pending.get(request_id)
            if item:
                item["approved"] = True
                item["event"].set()
                return True
            return False
        if self._pending:
            last_key = list(self._pending.keys())[-1]
            item = self._pending[last_key]
            item["approved"] = True
            item["event"].set()
            return True
        return False

    def deny(self, request_id: str | None = None) -> bool:
        if request_id:
            item = self._pending.get(request_id)
            if item:
                item["approved"] = False
                item["event"].set()
                return True
            return False
        if self._pending:
            last_key = list(self._pending.keys())[-1]
            item = self._pending[last_key]
            item["approved"] = False
            item["event"].set()
            return True
        return False

    def list_pending(self) -> list[dict[str, Any]]:
        return [
            {
                "id": k,
                "command": v["command"],
                "tier": v["tier"],
                "reason": v["reason"],
                "timestamp": v["timestamp"],
            }
            for k, v in self._pending.items()
        ]


_POLICY_ENGINE: CommandPolicyEngine | None = None


def get_command_policy_engine(workspace: Path | None = None) -> CommandPolicyEngine:
    global _POLICY_ENGINE
    if _POLICY_ENGINE is None:
        _POLICY_ENGINE = CommandPolicyEngine(workspace=workspace)
    return _POLICY_ENGINE


def bind_command_policy_engine(engine: CommandPolicyEngine | None) -> CommandPolicyEngine:
    global _POLICY_ENGINE
    _POLICY_ENGINE = engine if engine is not None else CommandPolicyEngine()
    return _POLICY_ENGINE
