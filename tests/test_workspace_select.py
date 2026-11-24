from robber.engine.model_ids import canonicalize_groq_model, completion_model_ids
from robber.router import clip_tool_context
from robber.tools.workspace import pick_read_target, select_tool


def test_read_intent_beats_file_token_directory_scan() -> None:
    query = (
        r'D:\Users\SAIF ALI KHAN\Desktop\ROBBER_AUDIT_server_and_tests.txt '
        "read this file and tel me what is in this"
    )
    assert select_tool(query) == "Read File"
    target = pick_read_target(query)
    assert target is not None
    assert target.endswith("ROBBER_AUDIT_server_and_tests.txt")
    assert "Desktop" in target


def test_scan_prompt_still_lists_directory() -> None:
    assert select_tool("scan the project files") == "Local Directory Scan"


def test_hi_is_conversation_not_workspace_dump() -> None:
    assert select_tool("hi") == "Conversation"
    assert select_tool("hello") == "Conversation"
    assert select_tool("search the web for python 3.12") == "Web Search"
    assert select_tool("show my desktop files") == "Host Files"
    assert select_tool("run whoami in the terminal") == "Command Runner"
    assert select_tool("cd .. && pwd") == "Command Runner"
    assert select_tool(r"scan H:\The Agent Robber UI Design Sample") == "Local Directory Scan"
    assert select_tool("can you code files directly in this machine") == "Conversation"
    assert select_tool("Create a test.py file with a hello world print") == "Direct Execution"


def test_groq_model_aliases() -> None:
    assert canonicalize_groq_model("llama-3.3") == "llama-3.3-70b-versatile"
    assert canonicalize_groq_model("qwen-2.5-coder-32b-instruct") == "qwen/qwen3-32b"
    http_model, litellm = completion_model_ids(
        model_string="llama-3.3",
        litellm_model="openai/llama-3.3",
        api_base="https://api.groq.com/openai/v1",
    )
    assert http_model == "llama-3.3-70b-versatile"
    assert litellm == "groq/llama-3.3-70b-versatile"


def test_clip_tool_context_caps_ssot_blob() -> None:
    blob = "x" * 20_000
    out = clip_tool_context(blob)
    assert out is not None
    assert len(out) < 13_000
    assert "clipped" in out


def test_search_query_from_strips_prefix() -> None:
    from robber.tools.web_search import search_query_from

    assert search_query_from("search the web for rust ownership") == "rust ownership"


def test_pack_ssot_context_does_not_need_path_import_at_callsite() -> None:
    from robber.ssot_context import pack_ssot_context

    packed = pack_ssot_context("hi", window=64)
    assert packed.configured == 64
    assert isinstance(packed.text, str)
