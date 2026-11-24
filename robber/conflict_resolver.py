"""Autonomous Conflict Resolution Matrix (ACRM).

Detect logical, factual, and architectural contradictions between concurrent
specialist outputs and arbitrate with confidence scores before synthesis.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

FILE_CARD_PATH_RE = re.compile(r'"path"\s*:\s*"([^"]+)"')
DIFF_PATH_RE = re.compile(r"^\+\+\+ [ab]/(.+)$", re.MULTILINE)
NUMBER_CLAIM_RE = re.compile(
    r"\b([A-Za-z][\w-]{2,24})\b[^\n]{0,40}?\b(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
FORBID_RE = re.compile(
    r"\b(?:do not|don't|must not|never|avoid)\b[^\n]{0,80}",
    re.IGNORECASE,
)
REQUIRE_RE = re.compile(
    r"\b(?:must|always|required to|shall)\b[^\n]{0,80}",
    re.IGNORECASE,
)


@dataclass
class Conflict:
    kind: str
    left_node: str
    right_node: str
    detail: str
    confidence: float
    winner: str = ""

    def public(self) -> dict[str, Any]:
        return asdict(self)


class ConflictResolutionMatrix:
    """Score competing specialist recommendations and pick a winner per clash."""

    PRIORITY = ("review", "coding", "planning")

    def detect(self, outputs: Mapping[str, str]) -> list[Conflict]:
        rows = {key: (value or "").strip() for key, value in outputs.items() if (value or "").strip()}
        names = list(rows)
        conflicts: list[Conflict] = []
        for i, left in enumerate(names):
            for right in names[i + 1 :]:
                conflicts.extend(self._pair(left, rows[left], right, rows[right]))
        return conflicts

    def arbitrate(self, conflicts: Iterable[Conflict]) -> list[Conflict]:
        resolved: list[Conflict] = []
        for item in conflicts:
            winner = self._winner(item)
            resolved.append(
                Conflict(
                    kind=item.kind,
                    left_node=item.left_node,
                    right_node=item.right_node,
                    detail=item.detail,
                    confidence=item.confidence,
                    winner=winner,
                )
            )
        return resolved

    def reconcile(self, outputs: Mapping[str, str]) -> dict[str, str]:
        texts = {key: value for key, value in outputs.items()}
        conflicts = self.arbitrate(self.detect(texts))
        for clash in conflicts:
            if not clash.winner:
                continue
            loser = clash.right_node if clash.winner == clash.left_node else clash.left_node
            if loser not in texts or clash.winner not in texts:
                continue
            if clash.kind == "architectural":
                continue
            stamp = f"[ACRM] deferred to {clash.winner}: {clash.detail}"
            if stamp in texts[loser]:
                continue
            texts[loser] = f"{texts[loser].rstrip()}\n\n{stamp}"
        return texts

    def report(self, outputs: Mapping[str, str]) -> dict[str, Any]:
        conflicts = self.arbitrate(self.detect(outputs))
        return {
            "conflicts": [item.public() for item in conflicts],
            "count": len(conflicts),
            "kinds": sorted({item.kind for item in conflicts}),
        }

    def _pair(self, left: str, left_text: str, right: str, right_text: str) -> list[Conflict]:
        found: list[Conflict] = []
        left_paths = _paths(left_text)
        right_paths = _paths(right_text)
        if left_paths and right_paths and left_paths.isdisjoint(right_paths):
            if _looks_like_patch(left_text) and _looks_like_patch(right_text):
                found.append(
                    Conflict(
                        "architectural",
                        left,
                        right,
                        f"specialists target disjoint files ({sorted(left_paths)[:3]} vs {sorted(right_paths)[:3]})",
                        0.72,
                    )
                )
        left_nums = _number_claims(left_text)
        right_nums = _number_claims(right_text)
        for key, lval in left_nums.items():
            rval = right_nums.get(key)
            if rval is not None and rval != lval:
                found.append(
                    Conflict(
                        "factual",
                        left,
                        right,
                        f"disagree on {key}: {lval} vs {rval}",
                        0.8,
                    )
                )
        for forbid in FORBID_RE.findall(left_text)[:4]:
            needle = _subject(forbid)
            if needle and needle.lower() in right_text.lower() and REQUIRE_RE.search(right_text):
                found.append(
                    Conflict(
                        "logical",
                        left,
                        right,
                        f"constraint clash around '{needle}'",
                        0.66,
                    )
                )
        if left == "review" and not _review_ok(left_text) and _looks_like_patch(right_text):
            found.append(
                Conflict(
                    "logical",
                    left,
                    right,
                    "review blocked a patch the coder emitted",
                    0.7,
                )
            )
        if right == "review" and not _review_ok(right_text) and _looks_like_patch(left_text):
            found.append(
                Conflict(
                    "logical",
                    left,
                    right,
                    "review blocked a patch the coder emitted",
                    0.7,
                )
            )
        return found

    def _winner(self, clash: Conflict) -> str:
        if clash.kind == "logical" and "review" in {clash.left_node, clash.right_node}:
            return "review"
        if clash.kind == "architectural" and "coding" in {clash.left_node, clash.right_node}:
            return "coding"
        for name in self.PRIORITY:
            if name in {clash.left_node, clash.right_node}:
                return name
        return clash.left_node


def _paths(text: str) -> set[str]:
    found = {item.replace("\\", "/") for item in FILE_CARD_PATH_RE.findall(text or "")}
    found.update(item.replace("\\", "/") for item in DIFF_PATH_RE.findall(text or ""))
    return {item for item in found if item and not item.startswith("dev/")}


def _looks_like_patch(text: str) -> bool:
    blob = text or ""
    return "```diff" in blob or "+++ " in blob or '"path"' in blob


def _number_claims(text: str) -> dict[str, str]:
    claims: dict[str, str] = {}
    for key, value in NUMBER_CLAIM_RE.findall(text or ""):
        token = key.lower()
        if token in {"line", "lines", "file", "files", "port", "http", "sha"}:
            continue
        claims[token] = value
    return claims


def _subject(clause: str) -> str:
    words = re.findall(r"[A-Za-z][\w./-]+", clause or "")
    skip = {"do", "not", "don't", "must", "never", "avoid", "the", "a", "an", "to"}
    for word in words:
        if word.lower() not in skip:
            return word
    return ""


def _review_ok(text: str) -> bool:
    head = (text or "").strip().splitlines()
    if not head:
        return True
    return bool(re.match(r"^\s*(ok|lgtm|no issues|looks good|approved)\b", head[0], re.I))
