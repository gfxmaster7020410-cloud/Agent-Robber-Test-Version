from __future__ import annotations



import json

from pathlib import Path



from robber.config import RAM_GUARD_BYTES, WORKER_PROMPT_TOKEN_CAP

from robber.memory.context_compressor import ContextCompressor

from robber.memory.ram_guard import RamGuard, bind_ram_guard

from robber.memory.token_budgeter import TokenBudgeter

from robber.planner import FAST_DRAFT

from robber.router.model_router import ModelRouter

from robber.ssot_context import estimate_tokens

from robber.state_manager import StateManager





def teardown_function() -> None:

    bind_ram_guard(None)





def test_token_budgeter_respects_tiny_windows() -> None:

    budgeter = TokenBudgeter(48, hard_cap=48)

    assert budgeter.usable == 48

    text, tokens, clipped = budgeter.fit("state_graph " * 80)

    assert clipped is True

    assert tokens <= 48

    assert "clipped" in text





def test_token_budgeter_splits_window_into_quotas() -> None:

    budgeter = TokenBudgeter(3000)

    quotas = budgeter.quotas()

    assert quotas.system == 450

    assert quotas.goal == 600

    assert quotas.graph == 1050

    assert quotas.history == 900

    assert quotas.total == 3000

    packed = budgeter.pack(

        system="sys " * 400,

        goal="goal " * 500,

        graph="ast " * 900,

        history="log " * 700,

    )

    assert packed.tokens <= WORKER_PROMPT_TOKEN_CAP

    assert packed.clipped is True

    assert packed.section_tokens["system"] <= quotas.system

    assert packed.section_tokens["graph"] <= quotas.graph





def test_router_fits_prompt_under_worker_cap() -> None:

    fitted = ModelRouter().choose(task_type=FAST_DRAFT, prompt="word " * 20_000)

    assert fitted.clipped is True

    assert fitted.tokens <= WORKER_PROMPT_TOKEN_CAP





def test_compressor_writes_kv_summary_and_drops_raw_from_prompt(tmp_path: Path) -> None:

    manager = StateManager(tmp_path)

    parent = manager.put_task("inspect generated.py", status="completed")

    fat = (

        "updated generated.py\ndef leftover():\n    pass\nstdout:\n"

        + ("tool-output-line\n" * 900)

        + "FAILED tests/test_x.py\n"

    )

    assert estimate_tokens(fat) > 2000

    manager.add_artifact("run", body=fat, kind="loop", task_id=parent["id"], exit_code=1)

    compressor = ContextCompressor(tmp_path, manager=manager)

    saved = compressor.summarize_task(parent["id"], force=True)

    assert saved is not None

    payload = json.loads(compressor.summary_for(parent["id"]))

    assert payload["files_modified"]

    assert "leftover" in payload["key_decisions"]

    assert payload["test_status"] == "FAILED"

    child = manager.put_task(

        "mutate generated.py",

        depends_on=[parent["id"]],

        payload={"tag": "mutate", "outputs": ["generated.py"]},

    )

    from robber.context_assembly import ContextAssembler



    packed = ContextAssembler(tmp_path, manager=manager, window=WORKER_PROMPT_TOKEN_CAP).assemble(child)

    assert packed.tokens <= WORKER_PROMPT_TOKEN_CAP

    assert "summary:" in packed.text

    assert fat not in packed.text

    assert packed.text.count("tool-output-line") < 8





def test_ram_guard_reclaims_when_rss_hot(tmp_path: Path, monkeypatch) -> None:

    from robber.memory import ram_guard as ram_mod



    monkeypatch.setattr(ram_mod, "rss_bytes", lambda pid=None: RAM_GUARD_BYTES + 1024)

    monkeypatch.setattr(

        ram_mod,

        "reclaim_local",

        lambda ssot_path=None: {"gc": 3, "sqlite_shrink": True, "rust_caches": True},

    )

    guard = RamGuard(tmp_path)

    report = guard.maybe_reclaim()

    assert report["reclaimed"] is True

    assert report["sqlite_shrink"] is True

    assert report["rss"] > RAM_GUARD_BYTES

    cool = RamGuard(tmp_path, threshold=RAM_GUARD_BYTES * 4)

    monkeypatch.setattr(ram_mod, "rss_bytes", lambda pid=None: 1024)

    skipped = cool.maybe_reclaim()

    assert skipped["reclaimed"] is False

