from __future__ import annotations

from robber.conflict_resolver import ConflictResolutionMatrix


def test_detects_factual_logical_and_architectural() -> None:
    matrix = ConflictResolutionMatrix()
    report = matrix.report(
        {
            "planning": "latency budget 120\ndo not modify server.py",
            "coding": "```diff\n+++ b/mod.py\n+ok\n```\nlatency budget 80",
            "review": "Missing tests on the patch",
        }
    )
    assert report["count"] >= 1
    kinds = set(report["kinds"])
    assert kinds & {"factual", "logical", "architectural"}
    winners = {item["winner"] for item in report["conflicts"] if item.get("winner")}
    assert winners


def test_reconcile_keeps_coding_on_disjoint_files() -> None:
    matrix = ConflictResolutionMatrix()
    reconciled = matrix.reconcile(
        {
            "coding": "```diff\n+++ b/a.py\n+a\n```",
            "planning": "```diff\n+++ b/b.py\n+b\n```",
        }
    )
    assert "coding" in reconciled
    assert "planning" in reconciled


def test_reconcile_stamps_factual_loser() -> None:
    matrix = ConflictResolutionMatrix()
    reconciled = matrix.reconcile(
        {
            "planning": "latency budget 120",
            "coding": "latency budget 80",
        }
    )
    loser = reconciled["coding"] if "deferred to planning" in reconciled["coding"] else reconciled["planning"]
    assert "[ACRM]" in loser


def test_empty_outputs_are_clean() -> None:
    matrix = ConflictResolutionMatrix()
    assert matrix.report({})["count"] == 0
    assert matrix.reconcile({}) == {}
    assert matrix.report({"planning": "   "})["count"] == 0
