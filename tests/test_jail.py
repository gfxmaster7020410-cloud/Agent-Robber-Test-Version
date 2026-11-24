from pathlib import Path

from robber.jail import display_path, resolve_inside


def test_resolve_inside_allows_absolute_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    target = outside / "notes.txt"
    target.write_text("ok", encoding="utf-8")
    resolved = resolve_inside(root, str(target))
    assert resolved == target.resolve()


def test_resolve_inside_keeps_relative_under_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    nested = root / "src"
    nested.mkdir(parents=True)
    file = nested / "a.py"
    file.write_text("x", encoding="utf-8")
    assert resolve_inside(root, "src/a.py") == file.resolve()


def test_display_path_falls_back_to_absolute(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "x.txt"
    outside.write_text("x", encoding="utf-8")
    shown = display_path(outside, root)
    assert str(outside.resolve()) in shown.replace("/", "\\") or shown.endswith("x.txt")
