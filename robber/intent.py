"""Classify user prompts as inquiry (meta/capability) vs action (execute)."""

from __future__ import annotations

import re

INQUIRY = "inquiry"
ACTION = "action"

CHAT_RE = re.compile(
    r"^\s*(hi|hello|hey|yo|sup|thanks|thank you|ok+|okay|yes|no|start|"
    r"good morning|good evening|howdy)[\s!.]*$",
    re.IGNORECASE,
)
FILE_HINT = re.compile(
    r"(?:[\w.-]+/)*[\w.-]+\.(?:py|ts|tsx|js|jsx|mjs|cjs|json|md|txt|css|html|toml|ya?ml|rs|go|sql)\b",
    re.IGNORECASE,
)
ACTION_VERB = re.compile(
    r"\b("
    r"create|make|write|add|implement|scaffold|generate|fix|patch|refactor|"
    r"delete|remove|rename|move|run|execute|install|build|compile|apply|"
    r"edit|update|replace|insert|scan|list"
    r")\b",
    re.IGNORECASE,
)
POLITE_ACTION = re.compile(
    r"\b(?:can you|could you|would you|please)\s+"
    r"(?:please\s+)?(?:create|make|write|add|fix|implement|build|run|edit|delete|generate|patch|refactor|scan)\b",
    re.IGNORECASE,
)
CODE_A = re.compile(r"\bcode (?:a|an|the|me|this|my)\b", re.IGNORECASE)
CAPABILITY = re.compile(
    r"\b("
    r"can you|could you|are you able|do you (?:have|support|allow)|"
    r"what (?:can you|tools|are you)|how (?:do you|does this|does the)|"
    r"how do i|is it possible|are you allowed|"
    r"what(?:'s| is) (?:your|the) (?:access|capability|mode)"
    r")\b",
    re.IGNORECASE,
)
DELIVERABLE = re.compile(
    r"\b("
    r"navbar|component|function|class|module|script|hello world|"
    r"unit test|pull request|diff"
    r")\b",
    re.IGNORECASE,
)
PROJECT_SCOPE = re.compile(
    r"\b(project|architecture|codebase|app|application|scaffold|from scratch|multi-?file)\b",
    re.IGNORECASE,
)


def classify_intent(query: str) -> str:
    text = (query or "").strip()
    if not text:
        return INQUIRY
    if CHAT_RE.match(text):
        return INQUIRY
    if POLITE_ACTION.search(text) or CODE_A.search(text):
        return ACTION
    has_action = bool(ACTION_VERB.search(text))
    has_target = bool(FILE_HINT.search(text) or DELIVERABLE.search(text))
    if has_action and has_target:
        return ACTION
    if has_action and not CAPABILITY.search(text):
        return ACTION
    if CAPABILITY.search(text):
        return INQUIRY
    if text.endswith("?") and not has_action:
        return INQUIRY
    words = text.split()
    if len(words) <= 2 and not FILE_HINT.search(text) and not has_action:
        return INQUIRY
    return ACTION


def is_inquiry(query: str) -> bool:
    return classify_intent(query) == INQUIRY


def is_simple_file_task(query: str) -> bool:
    text = (query or "").strip()
    if classify_intent(text) != ACTION:
        return False
    if PROJECT_SCOPE.search(text):
        return False
    files = FILE_HINT.findall(text)
    if len(files) != 1:
        return False
    return len(text.split()) <= 40


def should_use_distributed_brain(query: str, *, tool_name: str, task_kind: str) -> bool:
    if is_inquiry(query):
        return False
    if tool_name == "Conversation":
        return False
    if is_simple_file_task(query):
        return False
    return task_kind in {"coding", "reasoning"}
