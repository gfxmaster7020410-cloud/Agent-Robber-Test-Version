"""Cross-repository context assembler for multi-workspace symbol navigation.

Assembles context snippets across multiple repositories within strict token budgets.
Provides jump-to-definition across repos and intelligent context synthesis for swarm workers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robber.jail import workspace_root
from robber.memory.vector_store import VectorMatch, get_vector_store
from robber.ssot_context import estimate_tokens

logger = logging.getLogger(__name__)

# Token budget for multi-repo context (strict limit)
MULTI_REPO_TOKEN_BUDGET = 1200


@dataclass
class SymbolLocation:
    """Exact symbol location across repositories."""
    
    repo_name: str
    file_path: str
    line_start: int
    line_end: int
    symbol_name: str
    symbol_kind: str  # "function", "class", "interface", "type"
    signature: str


@dataclass
class MultiRepoContext:
    """Assembled context from multiple repositories."""
    
    query: str
    snippets: list[ContextSnippet] = field(default_factory=list)
    total_tokens: int = 0
    repos_included: list[str] = field(default_factory=list)


@dataclass
class ContextSnippet:
    """Code snippet with metadata."""
    
    repo_name: str
    file_path: str
    content: str
    line_start: int
    line_end: int
    tokens: int
    relevance_score: float


class MultiRepoContextEngine:
    """Cross-repository context assembler with hybrid search."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        token_budget: int = MULTI_REPO_TOKEN_BUDGET,
    ) -> None:
        self.root = (root or workspace_root()).resolve()
        self.token_budget = token_budget
        self._indexed_repos: dict[str, Path] = {}
        self._vector_store = None

    def register_repo(self, name: str, path: Path) -> None:
        """Register a repository for cross-repo context assembly."""
        if not path.exists():
            logger.warning(f"Repository path does not exist: {path}")
            return
        
        self._indexed_repos[name] = path.resolve()
        logger.info(f"Registered repo '{name}' at {path}")

    def list_repos(self) -> list[str]:
        """Get list of registered repository names."""
        return list(self._indexed_repos.keys())

    def assemble_context(
        self,
        query: str,
        *,
        target_repos: list[str] | None = None,
        top_k: int = 5,
    ) -> MultiRepoContext:
        """Assemble context snippets from multiple repos within token budget.
        
        Args:
            query: User query or symbol reference
            target_repos: Optional list of repo names to search (None = all repos)
            top_k: Maximum number of snippets to return
            
        Returns:
            MultiRepoContext with ranked code snippets
        """
        context = MultiRepoContext(query=query)
        
        # Filter target repos
        repos_to_search = target_repos or self.list_repos()
        if not repos_to_search:
            logger.warning("No repositories registered for context assembly")
            return context

        # Perform hybrid search across repositories
        snippets = self._search_across_repos(query, repos_to_search, top_k)
        
        # Assemble within token budget
        for snippet in snippets:
            if context.total_tokens + snippet.tokens > self.token_budget:
                # Stop if budget exceeded
                break
            
            context.snippets.append(snippet)
            context.total_tokens += snippet.tokens
            
            if snippet.repo_name not in context.repos_included:
                context.repos_included.append(snippet.repo_name)

        logger.info(
            f"Assembled {len(context.snippets)} snippets from {len(context.repos_included)} repos "
            f"({context.total_tokens}/{self.token_budget} tokens)"
        )
        
        return context

    def _search_across_repos(
        self,
        query: str,
        repo_names: list[str],
        top_k: int,
    ) -> list[ContextSnippet]:
        """Perform hybrid search across multiple repositories."""
        all_snippets: list[ContextSnippet] = []
        
        for repo_name in repo_names:
            repo_path = self._indexed_repos.get(repo_name)
            if not repo_path:
                continue
            
            # Search this repo
            snippets = self._search_repo(query, repo_name, repo_path)
            all_snippets.extend(snippets)
        
        # Sort by relevance score
        all_snippets.sort(key=lambda s: s.relevance_score, reverse=True)
        
        return all_snippets[:top_k]

    def _search_repo(
        self,
        query: str,
        repo_name: str,
        repo_path: Path,
    ) -> list[ContextSnippet]:
        """Search within a single repository using hybrid search."""
        snippets: list[ContextSnippet] = []
        
        try:
            # Use vector search if available
            if self._vector_store is None:
                self._vector_store = get_vector_store(self.root)
                self._vector_store.initialize()
            
            # Vector search
            vector_matches = self._vector_store.search(query, top_k=3)
            
            for match in vector_matches:
                # Extract path relative to repo
                try:
                    rel_path = Path(match.path).relative_to(repo_path)
                except ValueError:
                    # Not in this repo
                    continue
                
                # Estimate tokens
                tokens = estimate_tokens(match.content)
                
                snippets.append(
                    ContextSnippet(
                        repo_name=repo_name,
                        file_path=str(rel_path),
                        content=match.content,
                        line_start=1,
                        line_end=match.content.count("\n") + 1,
                        tokens=tokens,
                        relevance_score=match.similarity,
                    )
                )
        
        except Exception as exc:
            logger.error(f"Error searching repo '{repo_name}': {exc}")
        
        return snippets

    def resolve_global_symbol(self, symbol_name: str) -> SymbolLocation | None:
        """Resolve symbol to exact file path, repo, and line range across all indexed repos.
        
        Provides 1-click jump-to-definition functionality.
        
        Args:
            symbol_name: Symbol identifier (function/class/interface name)
            
        Returns:
            SymbolLocation if found, None otherwise
        """
        # Query SSOT database for symbol across all repos
        try:
            from robber.ssot import get_index
            
            index = get_index(self.root)
            index.load()
            
            # Search for symbol in all indexed repos
            for repo_name, repo_path in self._indexed_repos.items():
                # Try to find symbol in this repo's SSOT
                repo_index = get_index(repo_path)
                repo_index.load()
                
                # Search chunks for symbol definition
                for chunk in repo_index.chunks:
                    if symbol_name in chunk.get("content", ""):
                        # Found potential match
                        return SymbolLocation(
                            repo_name=repo_name,
                            file_path=chunk.get("path", ""),
                            line_start=chunk.get("start_line", 1),
                            line_end=chunk.get("end_line", 1),
                            symbol_name=symbol_name,
                            symbol_kind="symbol",  # Would need AST parsing for exact kind
                            signature=chunk.get("content", "")[:200],
                        )
        
        except Exception as exc:
            logger.error(f"Error resolving symbol '{symbol_name}': {exc}")
        
        return None

    def format_context_for_prompt(self, context: MultiRepoContext) -> str:
        """Format multi-repo context for LLM prompt."""
        if not context.snippets:
            return f"Query: {context.query}\n\nNo relevant code found across registered repositories."
        
        lines = [
            f"Query: {context.query}",
            f"Context from {len(context.repos_included)} repositories: {', '.join(context.repos_included)}",
            f"Token budget: {context.total_tokens}/{self.token_budget}",
            "",
        ]
        
        for i, snippet in enumerate(context.snippets, 1):
            lines.append(f"--- Snippet {i}: {snippet.repo_name}/{snippet.file_path} ---")
            lines.append(f"Lines {snippet.line_start}-{snippet.line_end} (Relevance: {snippet.relevance_score:.3f})")
            lines.append("```")
            lines.append(snippet.content)
            lines.append("```")
            lines.append("")
        
        return "\n".join(lines)

    def stats(self) -> dict[str, Any]:
        """Get statistics about registered repos and indexing."""
        stats = {
            "registered_repos": len(self._indexed_repos),
            "repo_names": list(self._indexed_repos.keys()),
            "token_budget": self.token_budget,
        }
        
        if self._vector_store is not None:
            stats["vector_count"] = self._vector_store.count()
        
        return stats


# Singleton instance
_MULTI_REPO_ENGINE: MultiRepoContextEngine | None = None


def get_multi_repo_context_engine(root: Path | None = None) -> MultiRepoContextEngine:
    """Get or create singleton multi-repo context engine."""
    global _MULTI_REPO_ENGINE
    if _MULTI_REPO_ENGINE is None:
        _MULTI_REPO_ENGINE = MultiRepoContextEngine(root)
    return _MULTI_REPO_ENGINE


def close_multi_repo_context_engine() -> None:
    """Close singleton multi-repo context engine."""
    global _MULTI_REPO_ENGINE
    _MULTI_REPO_ENGINE = None
