"""Self-codebase protection: engine files cannot be mutated without ROBBER_ALLOW_SELF_EDIT."""

from __future__ import annotations

from pathlib import Path

import pytest

from robber.mutations import FileMutationManager
from robber.security.perimeter import (
    ALLOW_SELF_EDIT_ENV,
    SelfModificationProtectedError,
    agent_install_root,
    is_protected_path,
    validate_file_mutation,
)
from robber.tools.command_guard import sanitize_command


def test_ssot_db_py_is_protected() -> None:
    target = agent_install_root() / "robber" / "ssot_db.py"
    assert target.is_file()
    assert is_protected_path(target)
    with pytest.raises(SelfModificationProtectedError):
        validate_file_mutation(str(target), is_dev_mode=False)


def test_mutation_manager_leaves_ssot_db_untouched() -> None:
    root = agent_install_root()
    target = root / "robber" / "ssot_db.py"
    before = target.read_bytes()
    manager = FileMutationManager(root)
    try:
        with pytest.raises(SelfModificationProtectedError):
            manager.write_file("robber/ssot_db.py", "# self-edit blocked\n")
        assert target.read_bytes() == before
        assert "# self-edit blocked" not in target.read_text(encoding="utf-8")
    finally:
        if target.read_bytes() != before:
            target.write_bytes(before)
            pytest.fail("ssot_db.py was mutated and had to be restored")


def test_shell_redirect_to_engine_file_is_blocked() -> None:
    result = sanitize_command(
        "echo hacked > robber/ssot_db.py",
        workspace=agent_install_root(),
    )
    assert result.blocked is True
    assert result.ok is False
    assert "protected" in result.reason


def test_rm_rf_root_blocked() -> None:
    assert sanitize_command("rm -rf /").blocked
    assert sanitize_command("format C:").blocked
    assert sanitize_command("while true; do echo x; done").blocked


def test_allow_flag_permits_fake_agent_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBBER_AGENT_ROOT", str(tmp_path))
    pack = tmp_path / "robber"
    pack.mkdir()
    target = pack / "ssot_db.py"
    target.write_text("orig\n", encoding="utf-8")
    monkeypatch.delenv(ALLOW_SELF_EDIT_ENV, raising=False)
    with pytest.raises(SelfModificationProtectedError):
        validate_file_mutation(str(target))
    monkeypatch.setenv(ALLOW_SELF_EDIT_ENV, "1")
    assert validate_file_mutation(str(target), is_dev_mode=True) is True


def test_workspace_file_still_writable(tmp_path: Path) -> None:
    manager = FileMutationManager(tmp_path)
    mutation = manager.write_file("app.py", "print(1)\n", action="create")
    assert mutation.action == "create"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "print(1)\n"
