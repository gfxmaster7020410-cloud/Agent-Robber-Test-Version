import asyncio

from robber.engine.distributed_brain import (
    BrainAssignment,
    BrainNode,
    BrainSynthesizer,
    DistributedAgentBrain,
    NODE_PROMPTS,
    WorkerResult,
)
from robber.router import Engine


def _engine(label: str) -> Engine:
    return Engine(label, f"openai/{label}", "openai", "k")


def test_decompose_act_uses_three_nodes() -> None:
    brain = DistributedAgentBrain()
    names = [node.name for node in brain.decompose("refactor the python module", mode="act")]
    assert names == ["planning", "coding", "review"]


def test_decompose_plan_skips_coding() -> None:
    brain = DistributedAgentBrain()
    names = [node.name for node in brain.decompose("design the architecture", mode="plan")]
    assert names == ["planning", "review"]


def test_status_line_and_parallel_flag() -> None:
    assignment = BrainAssignment(
        nodes=[BrainNode("planning", "reasoning", NODE_PROMPTS["planning"])],
        engines={
            "planning": [_engine("deepseek-r1")],
            "coding": [_engine("qwen-coder")],
            "review": [_engine("llama-flash")],
        },
    )
    assert assignment.parallel is True
    line = assignment.status_line()
    assert line.startswith("✦ Initiating: Distributed Brain active")
    assert "Reasoning: deepseek-r1" in line
    assert "Coding: qwen-coder" in line
    assert "Review: llama-flash" in line


def test_synthesizer_merges_diffs_and_drops_duplicates() -> None:
    diff = "```diff\n--- a/x\n+++ b/x\n+ok\n```"
    results = [
        WorkerResult("planning", "r1", "1. Recall SSOT project context\nkeep this in plan mode"),
        WorkerResult("coding", "coder", diff),
        WorkerResult("review", "rev", diff + "\nOK"),
    ]
    synth = BrainSynthesizer()
    act = synth.merge(results, mode="act")
    assert act.count("```diff") == 1
    assert "Recall SSOT" not in act
    assert "+ok" in act
    plan = synth.merge(results, mode="plan")
    assert "keep this in plan mode" in plan
    assert "Recall SSOT" not in plan


def test_review_blocking_note_survives() -> None:
    merged = BrainSynthesizer().merge(
        [
            WorkerResult("coding", "c", "```diff\n+x\n```"),
            WorkerResult("review", "r", "Missing test coverage on apply_hunks"),
        ],
        mode="act",
    )
    assert "### Review" in merged
    assert "Missing test coverage" in merged


def test_parallel_run_and_ttft_failover() -> None:
    calls: list[str] = []

    async def collector(engine, query, mode, tool_context=None, ttft_seconds=None, timeout=None):
        calls.append(engine.label)
        if engine.label == "slow":
            raise TimeoutError("ttft")
        if "Node B" in query:
            return "", "```diff\n+from brain\n```"
        if "Node C" in query:
            return "", "OK"
        return "", "brief"

    async def go() -> str:
        brain = DistributedAgentBrain(collector=collector, ttft_seconds=0.2)
        assignment = BrainAssignment(
            nodes=[
                BrainNode("planning", "reasoning", NODE_PROMPTS["planning"]),
                BrainNode("coding", "coding", NODE_PROMPTS["coding"]),
                BrainNode("review", "general", NODE_PROMPTS["review"]),
            ],
            engines={
                "planning": [_engine("r1")],
                "coding": [_engine("slow"), _engine("coder")],
                "review": [_engine("rev")],
            },
        )
        return await brain.run("fix the bug", mode="act", assignment=assignment)

    text = asyncio.run(go())
    assert "slow" in calls and "coder" in calls
    assert "+from brain" in text


def test_nodes_carry_silent_act_directive() -> None:
    assert "DO NOT print internal workflow" in NODE_PROMPTS["coding"]
    assert "DO NOT print internal workflow" in NODE_PROMPTS["planning"]


def test_bad_request_swaps_without_error_spam() -> None:
    class BadRequestError(Exception):
        pass

    emits: list[tuple[str, str]] = []

    async def collector(engine, query, mode, tool_context=None, ttft_seconds=None, timeout=None):
        if engine.label == "bad":
            raise BadRequestError("400")
        return "", "ok from backup"

    async def emit(role: str, content: str, **kwargs) -> None:
        emits.append((role, content))

    async def go() -> str:
        brain = DistributedAgentBrain(collector=collector, ttft_seconds=0.2)
        assignment = BrainAssignment(
            nodes=[BrainNode("planning", "reasoning", NODE_PROMPTS["planning"])],
            engines={"planning": [_engine("bad"), _engine("ok")]},
        )
        return await brain.run("hi", mode="act", assignment=assignment, emit=emit)

    text = asyncio.run(go())
    assert "ok from backup" in text
    assert any("Swapped to auto-discovered [ok]" in msg for _, msg in emits)
    assert not any(role == "error" for role, _ in emits)
