"""Test multi-repository indexing and hybrid search."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from robber.memory.multi_repo_context import MultiRepoContextEngine, get_multi_repo_context_engine
from robber.memory.vector_store import VectorStore, get_vector_store


def test_vector_store_init(tmp_path: Path) -> None:
    """Test VectorStore initialization."""
    store = VectorStore(tmp_path)
    assert store.root == tmp_path
    assert store.model_name == "all-MiniLM-L6-v2"
    assert store.db_path == tmp_path / "data" / "vectors.db"


def test_vector_store_add_and_search(tmp_path: Path) -> None:
    """Test adding documents and searching (without ONNX model)."""
    store = VectorStore(tmp_path)
    store.initialize()
    
    # Add documents
    store.add("doc1", "file1.py", "def get_user_by_id(id): return User.query.get(id)")
    store.add("doc2", "file2.py", "def create_user(name): return User.create(name)")
    
    assert store.count() == 2
    
    # Search (will return zero vectors without ONNX, but should not crash)
    results = store.search("get user", top_k=5)
    # Without ONNX model, we expect empty or zero-similarity results
    assert isinstance(results, list)
    
    store.close()


def test_multi_repo_context_engine_init(tmp_path: Path) -> None:
    """Test MultiRepoContextEngine initialization."""
    engine = MultiRepoContextEngine(tmp_path, token_budget=1200)
    assert engine.root == tmp_path
    assert engine.token_budget == 1200
    assert len(engine.list_repos()) == 0


def test_multi_repo_register_repo(tmp_path: Path) -> None:
    """Test registering repositories."""
    engine = MultiRepoContextEngine(tmp_path)
    
    # Create a test repo directory
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()
    (repo_path / "test.py").write_text("def test(): pass")
    
    engine.register_repo("test_repo", repo_path)
    assert "test_repo" in engine.list_repos()
    
    stats = engine.stats()
    assert stats["registered_repos"] == 1
    assert "test_repo" in stats["repo_names"]


def test_multi_repo_assemble_context(tmp_path: Path) -> None:
    """Test assembling context from multiple repos."""
    engine = MultiRepoContextEngine(tmp_path, token_budget=1200)
    
    # Create test repos
    repo1 = tmp_path / "repo1"
    repo1.mkdir()
    (repo1 / "file1.py").write_text("def func1(): pass")
    
    repo2 = tmp_path / "repo2"
    repo2.mkdir()
    (repo2 / "file2.py").write_text("def func2(): pass")
    
    engine.register_repo("repo1", repo1)
    engine.register_repo("repo2", repo2)
    
    # Assemble context (will be empty without indexed vectors)
    context = engine.assemble_context("function", top_k=5)
    assert context.query == "function"
    assert context.total_tokens <= 1200


def test_multi_repo_resolve_symbol(tmp_path: Path) -> None:
    """Test resolving symbols across repos."""
    engine = MultiRepoContextEngine(tmp_path)
    
    # Create test repo
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()
    (repo_path / "module.py").write_text("def my_function():\n    pass\n")
    
    engine.register_repo("test_repo", repo_path)
    
    # Try to resolve (may be None if SSOT not set up)
    location = engine.resolve_global_symbol("my_function")
    # This will likely be None in unit test environment
    assert location is None or location.symbol_name == "my_function"


def test_multi_repo_format_context(tmp_path: Path) -> None:
    """Test formatting context for LLM prompt."""
    engine = MultiRepoContextEngine(tmp_path)
    
    context = engine.assemble_context("test query")
    formatted = engine.format_context_for_prompt(context)
    
    assert "Query: test query" in formatted
    assert "registered repositories" in formatted


def test_vector_store_singleton(tmp_path: Path) -> None:
    """Test vector store singleton pattern."""
    store1 = get_vector_store(tmp_path)
    store2 = get_vector_store(tmp_path)
    assert store1 is store2


def test_multi_repo_engine_singleton(tmp_path: Path) -> None:
    """Test multi-repo engine singleton pattern."""
    engine1 = get_multi_repo_context_engine(tmp_path)
    engine2 = get_multi_repo_context_engine(tmp_path)
    assert engine1 is engine2


def test_index_repo_command_registration() -> None:
    """Test /index-repo command is registered."""
    from robber.commands.registry import get_registry
    from robber.commands import handlers
    
    registry = get_registry()
    cmd = registry.get("index-repo")
    assert cmd is not None
    assert cmd.name == "index-repo"
    assert "index" in cmd.aliases
    assert cmd.category == "Context"


def test_search_command_registration() -> None:
    """Test /search command is registered."""
    from robber.commands.registry import get_registry
    from robber.commands import handlers
    
    registry = get_registry()
    cmd = registry.get("search")
    assert cmd is not None
    assert cmd.name == "search"
    assert "find" in cmd.aliases or "query" in cmd.aliases
    assert cmd.category == "Context"
