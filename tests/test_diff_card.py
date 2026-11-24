from robber.diff_card import (
    count_diff_stats,
    mini_diff_from_code,
    mini_diff_from_file_card,
    parse_fence_header,
    parse_file_card,
    preview_lines,
)


SAMPLE = """diff --git a/gui/src/lib/orchestration.ts b/gui/src/lib/orchestration.ts
--- a/gui/src/lib/orchestration.ts
+++ b/gui/src/lib/orchestration.ts
@@ -1,4 +1,12 @@
-// raw response stream
+import { stripThoughts } from './parser';
+two
+three
+four
+five
+six
+seven
+eight
+nine
+ten
+eleven
+twelve
"""


def test_count_diff_stats_ignores_headers() -> None:
    added, removed = count_diff_stats(SAMPLE)
    assert added == 12
    assert removed == 1


def test_preview_is_capped() -> None:
    rows = preview_lines(SAMPLE, limit=4)
    assert len(rows) == 4
    assert rows[0] == ("del", "-// raw response stream")
    assert rows[1][0] == "add"


def test_mini_diff_from_unified_extracts_path() -> None:
    model = mini_diff_from_code(SAMPLE, language="diff")
    assert model is not None
    assert model.path == "gui/src/lib/orchestration.ts"
    assert model.added == 12
    assert model.removed == 1
    assert model.marker == "~"
    assert len(model.preview) <= 4


def test_fence_header_path_and_language() -> None:
    language, path = parse_fence_header("python:src/app.py")
    assert language == "python"
    assert path == "src/app.py"
    language, path = parse_fence_header("diff path=gui/src/lib/orchestration.ts")
    assert language == "diff"
    assert path == "gui/src/lib/orchestration.ts"


def test_file_card_create_preview() -> None:
    model = mini_diff_from_file_card(
        {
            "action": "create",
            "path": "hello.py",
            "after": "print('hi')\nprint('bye')\n",
        }
    )
    assert model.marker == "+"
    assert model.added >= 2
    assert model.removed == 0
    assert model.preview[0][0] == "add"


def test_parse_file_card_json() -> None:
    row = parse_file_card('{"action": "edit", "path": "a.ts", "before": "x", "after": "y"}')
    assert row is not None
    assert row["path"] == "a.ts"


def test_plain_python_is_not_a_file_card() -> None:
    assert mini_diff_from_code("print('hi')\n", language="python") is None
