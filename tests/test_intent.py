from robber.intent import (
    classify_intent,
    is_inquiry,
    is_simple_file_task,
    should_use_distributed_brain,
)
from robber.tools.workspace import select_tool, simple_new_file


def test_capability_question_is_inquiry() -> None:
    query = "can you code files directly in this machine"
    assert classify_intent(query) == "inquiry"
    assert is_inquiry(query)
    assert select_tool(query) == "Conversation"
    assert not should_use_distributed_brain(query, tool_name="Conversation", task_kind="coding")


def test_how_and_what_questions_are_inquiry() -> None:
    assert select_tool("What tools do you have?") == "Conversation"
    assert select_tool("How does this work?") == "Conversation"
    assert select_tool("How do I configure X?") == "Conversation"


def test_create_hello_world_is_direct_execution() -> None:
    query = "Create a test.py file with a hello world print"
    assert classify_intent(query) == "action"
    assert select_tool(query) == "Direct Execution"
    assert is_simple_file_task(query)
    assert simple_new_file(query) == ("test.py", 'print("Hello, World!")\n')
    assert not should_use_distributed_brain(
        query, tool_name="Direct Execution", task_kind="coding"
    )


def test_explicit_coding_tasks_are_action() -> None:
    assert classify_intent("Fix bug in pref.tsx") == "action"
    assert select_tool("Fix bug in pref.tsx") == "Direct Execution"
    assert select_tool("Create a React navbar") == "Direct Execution"
    assert select_tool("Run git status") == "Git Status Probe"


def test_polite_create_is_still_action() -> None:
    assert classify_intent("Can you create a test.py file with hello world") == "action"
    assert select_tool("Can you create a test.py file with hello world") == "Direct Execution"
