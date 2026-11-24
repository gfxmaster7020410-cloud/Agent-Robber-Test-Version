#!/usr/bin/env python3
"""
Verification script for Multi-Repository Indexing & Hybrid Search.

This script performs a comprehensive system check to ensure all modules are properly
installed, configured, and operational.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def main() -> int:
    print("=" * 80)
    print("Agent Robber - Multi-Repository Indexing & Hybrid Search Verification")
    print("=" * 80)
    print()

    checks_passed = 0
    checks_total = 0

    # Check 1: Rust modules
    checks_total += 1
    print("[1/10] Checking Rust multi_repo module...")
    try:
        # Check if Rust code compiles
        import subprocess
        result = subprocess.run(
            ["cargo", "check", "--manifest-path=indexer/Cargo.toml"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            print("  [OK] Rust indexer compiles successfully")
            checks_passed += 1
        else:
            print(f"  [FAIL] Rust compilation failed")
            print(f"    {result.stderr[:200]}")
    except Exception as exc:
        print(f"  [FAIL] Rust check failed: {exc}")

    # Check 2: Python vector_store module
    checks_total += 1
    print("[2/10] Checking vector_store module...")
    try:
        from robber.memory.vector_store import VectorStore, get_vector_store
        print("  [OK] vector_store module imports")
        checks_passed += 1
    except ImportError as exc:
        print(f"  [FAIL] vector_store import failed: {exc}")

    # Check 3: Python multi_repo_context module
    checks_total += 1
    print("[3/10] Checking multi_repo_context module...")
    try:
        from robber.memory.multi_repo_context import (
            MultiRepoContextEngine,
            get_multi_repo_context_engine,
        )
        print("  [OK] multi_repo_context module imports")
        checks_passed += 1
    except ImportError as exc:
        print(f"  [FAIL] multi_repo_context import failed: {exc}")

    # Check 4: VectorStore initialization
    checks_total += 1
    print("[4/10] Testing VectorStore initialization...")
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            store = VectorStore(Path(tmpdir))
            store.initialize()
            print(f"  [OK] VectorStore initialized (DB: {store.db_path})")
            store.close()
        checks_passed += 1
    except Exception as exc:
        print(f"  [FAIL] VectorStore initialization failed: {exc}")

    # Check 5: MultiRepoContextEngine initialization
    checks_total += 1
    print("[5/10] Testing MultiRepoContextEngine initialization...")
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = MultiRepoContextEngine(Path(tmpdir), token_budget=1200)
            stats = engine.stats()
            print(f"  [OK] Engine initialized (Token budget: {stats['token_budget']})")
        checks_passed += 1
    except Exception as exc:
        print(f"  [FAIL] Engine initialization failed: {exc}")

    # Check 6: /index-repo command registration
    checks_total += 1
    print("[6/10] Checking /index-repo command registration...")
    try:
        from robber.commands.registry import get_registry
        from robber.commands import handlers
        
        registry = get_registry()
        cmd = registry.get("index-repo")
        if cmd is not None:
            print(f"  [OK] /index-repo command registered (aliases: {', '.join(cmd.aliases)})")
            checks_passed += 1
        else:
            print("  [FAIL] /index-repo command not registered")
    except Exception as exc:
        print(f"  [FAIL] Command registration check failed: {exc}")

    # Check 7: /search command registration
    checks_total += 1
    print("[7/10] Checking /search command registration...")
    try:
        from robber.commands.registry import get_registry
        from robber.commands import handlers
        
        registry = get_registry()
        cmd = registry.get("search")
        if cmd is not None:
            print(f"  [OK] /search command registered (aliases: {', '.join(cmd.aliases)})")
            checks_passed += 1
        else:
            print("  [FAIL] /search command not registered")
    except Exception as exc:
        print(f"  [FAIL] Command registration check failed: {exc}")

    # Check 8: ONNX dependencies
    checks_total += 1
    print("[8/10] Checking ONNX dependencies...")
    try:
        import onnxruntime as ort
        import transformers
        import numpy as np
        
        print(f"  [OK] onnxruntime={ort.__version__}")
        print(f"  [OK] transformers={transformers.__version__}")
        print(f"  [OK] numpy={np.__version__}")
        checks_passed += 1
    except ImportError as exc:
        print(f"  [WARN] ONNX dependencies not fully installed: {exc}")
        print("         Dense vector search will be disabled")
        print("         Install with: pip install onnxruntime transformers numpy")

    # Check 9: Vector embedding test (without model)
    checks_total += 1
    print("[9/10] Testing vector embedding (fallback mode)...")
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            store = VectorStore(Path(tmpdir))
            store.initialize()
            
            # Test embedding (will return zero vectors without ONNX model)
            embeddings = store.embed(["test text"])
            if len(embeddings) == 1 and len(embeddings[0]) == 384:
                print(f"  [OK] Embedding generated (dim: {len(embeddings[0])})")
                checks_passed += 1
            else:
                print(f"  [FAIL] Embedding dimension mismatch")
            
            store.close()
    except Exception as exc:
        print(f"  [FAIL] Embedding test failed: {exc}")

    # Check 10: Hybrid search integration
    checks_total += 1
    print("[10/10] Testing hybrid search integration...")
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = MultiRepoContextEngine(Path(tmpdir))
            
            # Create a test repo
            test_repo = Path(tmpdir) / "test_repo"
            test_repo.mkdir()
            (test_repo / "test.py").write_text("def test_function():\n    pass\n")
            
            engine.register_repo("test_repo", test_repo)
            
            # Try to assemble context (will be empty without indexed vectors)
            context = engine.assemble_context("test", top_k=5)
            print(f"  [OK] Context assembled (query: {context.query})")
            checks_passed += 1
    except Exception as exc:
        print(f"  [FAIL] Hybrid search test failed: {exc}")

    print()
    print("=" * 80)
    print(f"Verification Results: {checks_passed}/{checks_total} checks passed")
    print("=" * 80)

    if checks_passed == checks_total:
        print()
        print("[SUCCESS] All checks passed - Multi-repo search is ready for use!")
        print()
        print("Try it now:")
        print("  python robber/launcher.py --skip-build")
        print("  Then in TUI:")
        print("    /index-repo ./gateway")
        print("    /search WebSocket IPC bridge")
        print()
        return 0
    else:
        print()
        print(f"[WARN] {checks_total - checks_passed} check(s) failed or skipped")
        print()
        if checks_passed >= 7:
            print("Core functionality available (BM25 search)")
            print("Install ONNX dependencies for dense vector search:")
            print("  pip install onnxruntime>=1.16 transformers>=4.35 numpy>=1.24")
        print()
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
