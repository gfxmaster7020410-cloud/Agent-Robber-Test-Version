"""Isolated Git workspace, branch, and AST-driven conventional commits.

Porcelain uses the local `git` binary (subprocess) so the 8GB RAM / i5 bound
does not pay for libgit2. AST classification prefers the Rust indexer when
the cdylib is present, then falls back to `robber.engine.git_engine`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from robber.engine.git_engine import GitEngine, conventional_message
from robber.jail import workspace_root
from robber.security.perimeter import (
    SelfModificationProtectedError,
    validate_file_mutation,
)

logger = logging.getLogger(__name__)

SESSION_FILE = ".robber/git_session.json"
AUTO_PR_ENV = "ROBBER_AUTO_PR"
AGENT_BRANCH_PREFIX = "agent/"

_TEST_NAME = re.compile(
    r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]+$|_test\.(py|go|rs)$|\.test\.(ts|tsx|js|jsx)$",
    re.I,
)


@dataclass
class GitStatus:
    branch: str = ""
    default_branch: str = "main"
    clean: bool = True
    porcelain: str = ""
    stash_count: int = 0
    agent_branches: list[str] = field(default_factory=list)
    ahead: int = 0
    behind: int = 0

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BranchSession:
    task_id: str
    branch: str
    original_branch: str
    stash_ref: str = ""
    stashed: bool = False

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CommitResult:
    ok: bool
    message: str = ""
    branch: str = ""
    sha: str = ""
    files: list[str] = field(default_factory=list)
    output: str = ""
    kind: str = ""

    def public(self) -> dict[str, Any]:
        return asdict(self)


class GitManager:
    """Task-scoped git workflow: isolate branch, commit AST diffs, restore."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        manager: Any = None,
    ) -> None:
        self.root = Path(root).resolve() if root else workspace_root()
        self.manager = manager
        self._engine = GitEngine(self.root)
        self._git = shutil.which("git") or "git"

    def available(self) -> bool:
        return bool(shutil.which("git"))

    def is_repo(self) -> bool:
        return (self.root / ".git").exists()

    def status(self) -> GitStatus:
        branch = self.current_branch() or "HEAD"
        porcelain = self._out(["status", "--porcelain"])
        stash = self._out(["stash", "list"])
        agent = [
            line.strip().lstrip("* ").strip()
            for line in self._out(["branch", "--list", "agent/*"]).splitlines()
            if line.strip()
        ]
        ahead, behind = 0, 0
        tracking = self._out(["status", "-sb"], check=False)
        ahead_m = re.search(r"ahead (\d+)", tracking)
        behind_m = re.search(r"behind (\d+)", tracking)
        if ahead_m:
            ahead = int(ahead_m.group(1))
        if behind_m:
            behind = int(behind_m.group(1))
        return GitStatus(
            branch=branch,
            default_branch=self.default_branch(),
            clean=not porcelain.strip(),
            porcelain=porcelain.strip(),
            stash_count=len([line for line in stash.splitlines() if line.strip()]),
            agent_branches=agent,
            ahead=ahead,
            behind=behind,
        )

    def current_branch(self) -> str:
        text = self._out(["rev-parse", "--abbrev-ref", "HEAD"], check=False).strip()
        return "" if text in {"HEAD", ""} else text

    def default_branch(self) -> str:
        for name in ("main", "master"):
            code, _ = self._run(["show-ref", "--verify", "--quiet", f"refs/heads/{name}"], check=False)
            if code == 0:
                return name
        symbolic = self._out(["symbolic-ref", "refs/remotes/origin/HEAD"], check=False).strip()
        if symbolic:
            return symbolic.rsplit("/", 1)[-1]
        return self.current_branch() or "main"

    def create_feature_branch(self, task_id: str, slug: str) -> str:
        """Stash dirty work if needed and checkout `agent/feat-[task_id]-[slug]`."""
        self._ensure_repo()
        branch = self.feature_branch_name(task_id, slug)
        original = self.current_branch() or self.default_branch()
        if original == branch:
            self._remember(BranchSession(task_id=str(task_id), branch=branch, original_branch=original))
            self._record("git branch", f"already on {branch}", branch=branch, task_id=str(task_id))
            return branch

        porcelain = self._out(["status", "--porcelain"])
        stash_ref = ""
        stashed = False
        if porcelain.strip():
            stamp = f"agent-robber:stash-{_short(task_id)}"
            self._run(["stash", "push", "-u", "-m", stamp], check=True)
            stash_ref = stamp
            stashed = True

        existing = self._out(["branch", "--list", branch]).strip()
        if existing:
            self._run(["checkout", branch], check=True)
        else:
            self._run(["checkout", "-b", branch], check=True)

        session = BranchSession(
            task_id=str(task_id or ""),
            branch=branch,
            original_branch=original,
            stash_ref=stash_ref,
            stashed=stashed,
        )
        self._remember(session)
        self._upsert_ssot_branch(session, status="active")
        self._record("git branch", f"isolated {branch} from {original}", branch=branch, task_id=str(task_id))
        return branch

    def restore_workspace(self, *, drop_branch: bool = False) -> dict[str, Any]:
        session = self.load_session()
        target = (session.original_branch if session else None) or self.default_branch()
        current = self.current_branch()
        if current and current != target:
            self._run(["checkout", target], check=False)
        popped = False
        if session and session.stashed and session.stash_ref:
            code, _ = self._run(["stash", "pop"], check=False)
            popped = code == 0
        dropped = False
        if drop_branch and session and session.branch.startswith(AGENT_BRANCH_PREFIX):
            self._run(["branch", "-D", session.branch], check=False)
            dropped = True
        if session:
            self._upsert_ssot_branch(session, status="restored")
        self._clear_session()
        return {
            "ok": True,
            "branch": self.current_branch(),
            "restored": target,
            "stash_popped": popped,
            "dropped": dropped,
        }

    def feature_branch_name(self, task_id: str, slug: str) -> str:
        try:
            from robber.indexer_bridge import feature_branch_name as native

            name = native(task_id, slug)
            if name:
                return name
        except Exception:
            pass
        ident = _slug(task_id)
        text = _slug(slug)[:40] or "change"
        if not ident or ident == text or (len(ident) >= 8 and ident.isalnum() and all(c in "0123456789abcdef" for c in ident)):
            return f"agent/feat-{text}"
        return f"agent/feat-{ident[:12]}-{text}"

    def conventional_commit(
        self,
        paths: list[str] | None = None,
        *,
        task_id: str | None = None,
        message: str = "",
        scope: str = "",
    ) -> CommitResult:
        self._ensure_repo()
        rels = self._safe_paths(paths)
        rels = self._filter_protected(rels)
        if not rels:
            changed = self._changed_paths()
            rels = self._filter_protected(changed)
        if not rels:
            return CommitResult(ok=False, output="nothing to commit", branch=self.current_branch())

        kind, subject, footer = self._compose_message(rels, scope=scope, override=message)
        full = subject if not footer else f"{subject}\n\n{footer}"
        report = self._engine.commit_paths(rels, message=full)
        sha = self._out(["rev-parse", "HEAD"], check=False).strip()
        branch = self.current_branch()
        result = CommitResult(
            ok=bool(report.get("ok")),
            message=subject,
            branch=branch,
            sha=sha,
            files=rels,
            output=str(report.get("output") or ""),
            kind=kind,
        )
        if result.ok:
            self._record(
                "git commit",
                subject,
                branch=branch,
                task_id=task_id,
                payload=json.dumps(result.public(), ensure_ascii=True)[:4000],
            )
            session = self.load_session()
            if session:
                self._upsert_ssot_branch(session, status="committed", commit_sha=sha)
        return result

    def changed_files(self) -> list[str]:
        return self._changed_paths()

    def load_session(self) -> BranchSession | None:
        path = self.root / SESSION_FILE
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not data.get("branch"):
            return None
        return BranchSession(
            task_id=str(data.get("task_id") or ""),
            branch=str(data.get("branch") or ""),
            original_branch=str(data.get("original_branch") or ""),
            stash_ref=str(data.get("stash_ref") or ""),
            stashed=bool(data.get("stashed")),
        )

    def _compose_message(
        self,
        paths: list[str],
        *,
        scope: str,
        override: str,
    ) -> tuple[str, str, str]:
        if override.strip():
            kind = override.split(":", 1)[0].strip() or "chore"
            return kind, override.strip(), self._footer(paths)

        bases: list[str] = []
        ours: list[str] = []
        for rel in paths[:6]:
            dest = self.root / rel
            ours.append(dest.read_text(encoding="utf-8", errors="replace") if dest.is_file() else "")
            bases.append(self._show("HEAD", rel))
        joined_base = "\n".join(bases)
        joined_ours = "\n".join(ours)
        scope_text = scope or _scope_from_paths(paths)
        try:
            from robber.indexer_bridge import conventional_commit as native

            subject = native(joined_base, joined_ours, "\n".join(paths), scope_text)
            if subject:
                kind = subject.split(":", 1)[0].split("(", 1)[0].strip() or "chore"
                return kind, subject, self._footer(paths, joined_base, joined_ours)
        except Exception:
            pass
        kind = _classify_python(paths, joined_base, joined_ours)
        rest = conventional_message(joined_base, joined_ours)
        body = rest.split(":", 1)[-1].strip() if ":" in rest else rest
        if scope_text:
            subject = f"{kind}({scope_text}): {body}"
        else:
            subject = f"{kind}: {body}"
        return kind, subject, self._footer(paths, joined_base, joined_ours)

    def _footer(self, paths: list[str], base: str = "", ours: str = "") -> str:
        lines = [f"Files: {', '.join(paths[:8])}"]
        added = _symbol_delta(base, ours)
        if added:
            lines.append(f"Added symbols: {', '.join(added[:8])}")
        return "\n".join(lines)

    def _filter_protected(self, paths: list[str]) -> list[str]:
        allowed: list[str] = []
        for rel in paths:
            dest = self.root / rel
            try:
                validate_file_mutation(str(dest))
            except SelfModificationProtectedError:
                logger.info("git skipped protected path %s", rel)
                continue
            allowed.append(rel)
        return allowed

    def _safe_paths(self, paths: list[str] | None) -> list[str]:
        out: list[str] = []
        for item in paths or []:
            text = str(item or "").replace("\\", "/").strip()
            if not text or ".." in Path(text).parts:
                continue
            dest = Path(text)
            full = dest if dest.is_absolute() else (self.root / dest)
            try:
                rel = full.resolve().relative_to(self.root.resolve()).as_posix()
            except ValueError:
                continue
            out.append(rel)
        return out

    def _changed_paths(self) -> list[str]:
        raw = self._out(["diff", "--name-only", "HEAD"], check=False)
        untracked = self._out(["ls-files", "--others", "--exclude-standard"], check=False)
        names = [line.strip().replace("\\", "/") for line in (raw + "\n" + untracked).splitlines() if line.strip()]
        seen: list[str] = []
        for name in names:
            if name not in seen:
                seen.append(name)
        return seen

    def _show(self, rev: str, rel: str) -> str:
        code, blob = self._run(["show", f"{rev}:{rel}"], check=False)
        return blob if code == 0 else ""

    def _ensure_repo(self) -> None:
        if not self.available():
            raise RuntimeError("git not installed")
        if not self.is_repo():
            self._run(["init", "-q"], check=True)
            self._run(["-c", "user.name=Agent Robber", "-c", "user.email=robber@local", "commit", "--allow-empty", "--no-gpg-sign", "-m", "chore: seed workspace"], check=False)

    def _remember(self, session: BranchSession) -> None:
        path = self.root / SESSION_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(session.public(), indent=2), encoding="utf-8")

    def _clear_session(self) -> None:
        path = self.root / SESSION_FILE
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass

    def _upsert_ssot_branch(
        self,
        session: BranchSession,
        *,
        status: str,
        commit_sha: str = "",
        pr_url: str = "",
    ) -> None:
        if self.manager is None:
            return
        try:
            db = self.manager.db
            if hasattr(db, "upsert_git_branch"):
                db.upsert_git_branch(
                    project_id=getattr(self.manager, "project_id", None),
                    task_id=session.task_id or None,
                    branch_name=session.branch,
                    base_branch=session.original_branch,
                    status=status,
                    stash_ref=session.stash_ref,
                    commit_sha=commit_sha,
                    pr_url=pr_url,
                )
        except Exception:
            logger.debug("ssot git branch skipped")

    def _record(
        self,
        choice: str,
        rationale: str,
        *,
        branch: str = "",
        task_id: str | None = None,
        payload: str = "",
    ) -> None:
        if self.manager is None:
            return
        try:
            self.manager.record_decision(
                choice,
                rationale,
                task_id=task_id,
                branch=branch or self.current_branch() or "git",
                payload=payload,
                version_tag="git",
            )
        except Exception:
            logger.debug("git decision skipped")

    def _out(self, args: list[str], *, check: bool = True) -> str:
        _, blob = self._run(args, check=check)
        return blob

    def _run(self, args: list[str], *, check: bool = True) -> tuple[int, str]:
        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", "Agent Robber")
        env.setdefault("GIT_AUTHOR_EMAIL", "robber@local")
        env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
        env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
        proc = subprocess.run(
            [self._git, *args],
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        blob = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if check and proc.returncode != 0:
            raise RuntimeError(blob or f"git {' '.join(args)} failed")
        return int(proc.returncode), blob


def wants_auto_pr(payload: dict[str, Any] | None = None) -> bool:
    if payload and bool(payload.get("auto_pr")):
        return True
    return (os.environ.get(AUTO_PR_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def slug_from_query(query: str) -> str:
    text = (query or "").strip()
    lower = text.lower()
    if "json" in lower and "log" in lower:
        return "json-logger-helper"
    text = re.sub(r"^(implement|add|create|build|write)\s+(a|an|the)\s+", "", text, flags=re.I)
    text = re.sub(r"^(implement|add|create|build|write)\s+", "", text, flags=re.I)
    return _slug(text)[:40] or "task"


def _slug(raw: str) -> str:
    parts = re.findall(r"[A-Za-z0-9]+", raw or "")
    return "-".join(p.lower() for p in parts)


def _short(task_id: str) -> str:
    return _slug(task_id)[:12] or "task"


def _scope_from_paths(paths: list[str]) -> str:
    if not paths:
        return ""
    name = Path(paths[0]).stem
    name = re.sub(r"^test_", "", name)
    name = re.sub(r"_test$", "", name)
    return _slug(name)[:24]


def _classify_python(paths: list[str], base: str, ours: str) -> str:
    if paths and all(_TEST_NAME.search(p.replace("\\", "/")) for p in paths):
        return "test"
    msg = conventional_message(base, ours)
    kind = msg.split(":", 1)[0].strip()
    if kind in {"feat", "fix", "refactor", "test"}:
        return kind
    lower = (ours + "\n" + " ".join(paths)).lower()
    if "fix" in lower or "bug" in lower:
        return "fix"
    return "chore"


def _symbol_delta(base: str, ours: str) -> list[str]:
    def names(src: str) -> set[str]:
        found = set(re.findall(r"^(?:def|class|fn|func|export function|export class)\s+(\w+)", src or "", re.M))
        return found

    return sorted(names(ours) - names(base))


def get_git_manager(root: Path | str | None = None, *, manager: Any = None) -> GitManager:
    return GitManager(root, manager=manager)
