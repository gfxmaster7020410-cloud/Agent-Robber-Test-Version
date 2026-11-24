"""File mutation manager: apply, undo, redo, accept, revert."""

from __future__ import annotations

from pathlib import Path

from robber.mutations import FileMutationManager, bind_mutations


def test_write_undo_redo_accept_revert(tmp_path: Path) -> None:
    root = tmp_path
    manager = bind_mutations(FileMutationManager(root))
    target = root / "pref.tsx"
    target.write_text("export const a = 1;\n", encoding="utf-8")

    manager.begin_turn()
    mutation = manager.write_file("pref.tsx", "export const a = 2;\n")
    assert mutation.action == "edit"
    assert target.read_text(encoding="utf-8") == "export const a = 2;\n"
    snap = manager.public()
    assert snap["can_undo"] is True
    assert snap["pending"] == 1
    assert snap["files"][0]["path"] == "pref.tsx"
    assert snap["files"][0]["marker"] == "~"
    assert snap["files"][0]["detail"] == "(+0, -0)"
    assert snap["files"][0]["line"].startswith("[~] pref.tsx")

    assert manager.undo().startswith("Undid")
    assert target.read_text(encoding="utf-8") == "export const a = 1;\n"
    assert manager.public()["can_redo"] is True

    assert manager.redo().startswith("Redid")
    assert target.read_text(encoding="utf-8") == "export const a = 2;\n"

    assert manager.accept_all().startswith("Accepted")
    assert manager.public()["files"][0]["status"] == "accepted"

    created = manager.write_file("new-file.ts", "console.log(1);\n", action="create")
    assert created.action == "create"
    assert (root / "new-file.ts").is_file()
    assert manager.revert_all().startswith("Reverted")
    assert target.read_text(encoding="utf-8") == "export const a = 2;\n"
    assert not (root / "new-file.ts").exists()
    assert manager.public()["files"] == []


def test_apply_json_card_and_empty_undo(tmp_path: Path) -> None:
    manager = bind_mutations(FileMutationManager(tmp_path))
    cards = manager.apply_from_text(
        '```json\n{"action": "create", "path": "hello.ts", "after": "export {}\\n"}\n```'
    )
    assert len(cards) == 1
    assert cards[0]["path"] == "hello.ts"
    assert "export {}" in (tmp_path / "hello.ts").read_text(encoding="utf-8")
    fresh = FileMutationManager(tmp_path)
    assert fresh.undo() == "Nothing to undo"


def test_log_stream_envelope() -> None:
    from robber.events import attach_sync_envelope, set_last_command, set_elapsed_ms

    set_last_command("/undo")
    set_elapsed_ms(927)
    frame = attach_sync_envelope({"type": "error", "content": "Unknown command: /undo"})
    assert frame["event"] == "LOG_STREAM"
    assert frame["payload"]["type"] == "error"
    assert frame["payload"]["command"] == "/undo"
    assert frame["payload"]["timestamp_ms"] == 927
    state = attach_sync_envelope({"type": "state", "mode": "act"})
    assert state["event"] == "STATE_SYNC"


def test_rollback_turn_restores_edit_and_deletes_create(tmp_path: Path) -> None:
    manager = bind_mutations(FileMutationManager(tmp_path))
    original = tmp_path / "keep.txt"
    original.write_text("before\n", encoding="utf-8")

    manager.begin_turn()
    manager.write_file("keep.txt", "during-turn\n")
    manager.write_file("created.txt", "new-file\n", action="create")
    assert original.read_text(encoding="utf-8") == "during-turn\n"
    assert (tmp_path / "created.txt").is_file()

    note = manager.rollback_turn()
    assert "Rolled back" in note
    assert original.read_text(encoding="utf-8") == "before\n"
    assert not (tmp_path / "created.txt").exists()
    assert manager.public()["files"] == []


def test_rollback_after_multi_file_apply_from_text(tmp_path: Path) -> None:
    manager = bind_mutations(FileMutationManager(tmp_path))
    (tmp_path / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
    manager.begin_turn()
    manager.apply_from_text(
        '```json\n{"action": "edit", "path": "a.ts", "after": "export const a = 2;\\n"}\n```\n'
        '```json\n{"action": "create", "path": "b.ts", "after": "export const b = 1;\\n"}\n```'
    )
    assert (tmp_path / "a.ts").read_text(encoding="utf-8") == "export const a = 2;\n"
    assert (tmp_path / "b.ts").is_file()
    manager.rollback_turn()
    assert (tmp_path / "a.ts").read_text(encoding="utf-8") == "export const a = 1;\n"
    assert not (tmp_path / "b.ts").exists()


def test_git_workspace_snapshot_roundtrip(tmp_path: Path) -> None:
    import os
    import shutil
    import subprocess

    import pytest

    from robber.patch import create_workspace_snapshot, restore_workspace_snapshot

    if not shutil.which("git"):
        pytest.skip("git is not installed")

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("committed\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True, capture_output=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env=env,
    )
    tracked.write_text("dirty-before-turn\n", encoding="utf-8")
    snap = create_workspace_snapshot(tmp_path)
    assert snap["oid"]
    tracked.write_text("mutated-during-turn\n", encoding="utf-8")
    restore_workspace_snapshot(tmp_path, snap)
    assert tracked.read_text(encoding="utf-8") == "dirty-before-turn\n"


def test_workspace_snapshot_skips_non_utf8_untracked(tmp_path: Path) -> None:
    import os
    import shutil
    import subprocess

    import pytest

    from robber.patch import create_workspace_snapshot

    if not shutil.which("git"):
        pytest.skip("git is not installed")

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "ok.txt").write_text("ascii\n", encoding="utf-8")
    (tmp_path / "latin1.txt").write_bytes(b"x" * 22 + b"\xd3\xff")
    (tmp_path / "font.ttf").write_bytes(b"\x00\x01\x00\x00" + b"\xd3" * 8)
    snap = create_workspace_snapshot(tmp_path)
    bodies = snap["untracked"]
    assert (bodies.get("ok.txt") or "").replace("\r", "").startswith("ascii")
    assert "latin1.txt" not in bodies
    assert "font.ttf" not in bodies
