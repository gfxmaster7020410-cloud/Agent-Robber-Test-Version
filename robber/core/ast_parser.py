from typing import Dict, Any, List, Optional
import os
import re
from pathlib import Path

# Attempt tree-sitter imports
try:
    import tree_sitter_python
    from tree_sitter import Language, Parser
except ImportError:
    tree_sitter_python = None
    Language = None
    Parser = None

class ASTCodeParser:
    """Handles AST parsing and line range extraction for source files."""
    
    def __init__(self):
        self.parser = None
        if Parser and tree_sitter_python:
            self.parser = Parser()
            try:
                self.parser.set_language(Language(tree_sitter_python.language()))
            except Exception:
                self.parser = None

    def extract_symbols(self, file_path: str) -> List[Dict[str, Any]]:
        """Extract symbols with precise line ranges."""
        path = Path(file_path)
        if not path.exists():
            return []
            
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
            lines = content.splitlines()
            
        symbols = []
        suffix = path.suffix.lower()
        
        # Try AST first if available
        if self.parser and suffix == '.py':
            tree = self.parser.parse(bytes(content, "utf-8"))
            for node in tree.root_node.children:
                if node.type in ['function_definition', 'class_definition']:
                    symbols.append({
                        'type': node.type,
                        'name': node.child_by_field_name('name').text.decode('utf-8') if node.child_by_field_name('name') else 'unknown',
                        'start_line': node.start_point[0] + 1,
                        'end_line': node.end_point[0] + 1
                    })
            return symbols
            
        # Fallback regex
        pattern_map = {
            '.py': r'^\s*(?:def|class)\s+(\w+)',
            '.js': r'(?:function|const|let|var|class)\s+(\w+)',
            '.ts': r'(?:function|const|let|var|class|interface|type)\s+(\w+)',
            '.json': None,
            '.md': None
        }
        
        regex = pattern_map.get(suffix)
        if not regex:
            return []

        for i, line in enumerate(lines):
            match = re.search(regex, line)
            if match:
                symbols.append({
                    'type': 'symbol',
                    'name': match.group(1),
                    'start_line': i + 1,
                    'end_line': i + 1 # Approximation for fallback
                })
                
        return symbols

    def extract_line_range(self, file_path: str, start_line: int, end_line: int) -> Dict[str, Any]:
        """Extracts a specific line range from a file with metadata."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
            
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
        start = max(0, start_line - 1)
        end = min(len(lines), end_line)
        
        content = "".join(lines[start:end])
        char_count = len(content)
        estimated_tokens = char_count // 4
        
        return {
            "content": content,
            "char_count": char_count,
            "estimated_tokens": estimated_tokens,
            "line_range": f"L{start_line}-L{end_line}"
        }
