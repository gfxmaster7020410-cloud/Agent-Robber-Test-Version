"""Autonomous git workflow, AST conventional commits, and PR synthesis."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from robber.agents.pr_writer import PRWriterWorker
from robber.security.perimeter import SelfModificationProtectedError, validate_file_mutation
from robber.tools.git_manager import GitManager, slug_from_query
from robber.tools.git_provider import GitProvider, PRResult


def _git_repo(tmp_path: Path) -> Path:
    if not shutil.which("git"):
        pytest.skip("git not installed")
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="Agent Robber",
        GIT_AUTHOR_EMAIL="robber@local",
        GIT_COMMITTER_NAME="Agent Robber",
        GIT_COMMITTER_EMAIL="robber@local",
    )
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=env)
    subprocess.run(
        ["git", "-c", "user.name=Agent Robber", "-c", "user.email=robber@local", "commit", "--allow-empty", "--no-gpg-sign", "-m", "init"],
        cwd=tmp_path,
        check=True,
        env=env,
        capture_output=True,
    )
    return tmp_path


def test_slug_from_json_log_query() -> None:
    assert slug_from_query("Implement a helper utility for parsing JSON logs, create unit tests, and open a PR.") == "json-logger-helper"


def test_feature_branch_name_drops_uuid() -> None:
    git = GitManager()
    name = git.feature_branch_name("a1b2c3d4e5f67890", "json-logger-helper")
    assert name == "agent/feat-json-logger-helper"


def test_create_feature_branch_and_restore(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "notes.txt").write_text("dirty", encoding="utf-8")
    git = GitManager(root)
    original = git.current_branch()
    branch = git.create_feature_branch("json-logger-helper", "json-logger-helper")
    assert branch == "agent/feat-json-logger-helper"
    assert git.current_branch() == branch
    session = git.load_session()
    assert session is not None
    assert session.stashed is True
    report = git.restore_workspace()
    assert report["ok"] is True
    assert git.current_branch() == original or git.current_branch() == report["restored"]


def test_ast_conventional_commit_feat(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    git = GitManager(root)
    git.create_feature_branch("task", "logger")
    (root / "logger.py").write_text(
        "class JsonLogParser:\n    def parse(self, line):\n        return line\n",
        encoding="utf-8",
    )
    result = git.conventional_commit(["logger.py"], scope="logger")
    assert result.ok is True, result.output
    assert result.message.startswith("feat")
    assert "JsonLogParser" in result.message or "json" in result.message.lower() or "feat" in result.message


def test_test_files_commit_as_test(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    git = GitManager(root)
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_logger.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    result = git.conventional_commit(["tests/test_logger.py"])
    assert result.ok is True, result.output
    assert result.kind == "test" or result.message.startswith("test")


def test_pr_writer_lists_added_symbols(tmp_path: Path) -> None:
    (tmp_path / "logger.py").write_text("class JsonLogParser:\n    pass\n", encoding="utf-8")
    doc = PRWriterWorker(tmp_path).synthesize(
        query="add json log parser",
        files=["logger.py"],
        commit_message="feat(logger): add JsonLogParser utility",
        branch="agent/feat-json-logger-helper",
    )
    assert "Summary of Changes" in doc.body
    assert "AST Symbol Changes" in doc.body
    assert any(item.name == "JsonLogParser" for item in doc.symbols)
    assert doc.title.startswith("feat")


def test_git_provider_dry_run(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    provider = GitProvider(root)
    result = provider.push_and_create_pr(
        "agent/feat-json-logger-helper",
        "feat(logger): add JsonLogParser utility",
        "body",
        dry_run=True,
    )
    assert isinstance(result, PRResult)
    assert result.ok is True
    assert result.branch == "agent/feat-json-logger-helper"


def test_protected_engine_path_blocked() -> None:
    from robber.security.perimeter import agent_install_root

    target = agent_install_root() / "robber" / "tools" / "git_manager.py"
    if not target.is_file():
        pytest.skip("engine file missing")
    old = os.environ.pop("ROBBER_ALLOW_SELF_EDIT", None)
    try:
        with pytest.raises(SelfModificationProtectedError):
            validate_file_mutation(str(target))
    finally:
        if old is not None:
            os.environ["ROBBER_ALLOW_SELF_EDIT"] = old


def test_git_and_pr_commands_registered() -> None:
    from robber.commands import handlers  # noqa: F401
    from robber.commands.registry import get_registry

    registry = get_registry()
    git = registry.get("git")
    pr = registry.get("pr")
    assert git is not None
    assert git.usage.startswith("/git")
    assert pr is not None
    assert "create" in pr.usage


def test_ssot_git_branches_table(tmp_path: Path) -> None:
    from robber.ssot_db import SsotDB

    db = SsotDB(tmp_path / "ssot.db")
    db.connect()
    row = db.upsert_git_branch(
        project_id=None,
        task_id="t1",
        branch_name="agent/feat-json-logger-helper",
        base_branch="main",
        status="active",
    )
    assert row["branch_name"] == "agent/feat-json-logger-helper"
    listed = db.list_git_branches()
    assert listed
    db.close()


def test_swarm_has_pr_writer(tmp_path: Path) -> None:
    from robber.agents.swarm_orchestrator import SwarmOrchestrator

    orch = SwarmOrchestrator(tmp_path)
    assert "pr_writer" in orch.workers
    assert orch.workers["pr_writer"].role == "pr_writer"
