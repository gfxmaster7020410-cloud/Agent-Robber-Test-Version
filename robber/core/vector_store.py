import hashlib
from pathlib import Path
from typing import Any, Dict, List

import lancedb
import pyarrow as pa
from fastembed import TextEmbedding

from .ast_parser import ASTCodeParser


class VectorIndexManager:
    """Manages local LanceDB vector storage."""

    def __init__(self, db_path: str = ".robber/index/lancedb"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = lancedb.connect(str(self.db_path))
        self.embedding_model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
        self.parser = ASTCodeParser()
        self._init_table()

    def _init_table(self):
        # Explicit PyArrow Schema required by LanceDB (384 dimensions for bge-small-en-v1.5)
        schema = pa.schema([
            pa.field("id", pa.string()),
            pa.field("file_path", pa.string()),
            pa.field("symbol_name", pa.string()),
            pa.field("content", pa.string()),
            pa.field("start_line", pa.int32()),
            pa.field("end_line", pa.int32()),
            pa.field("vector", pa.list_(pa.float32(), 384)),
        ])

        self.table = self.db.create_table("code_chunks", schema=schema, exist_ok=True)

    def index_file(self, file_path: str) -> int:
        """Parses file, generates embeddings, and upserts."""
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        chunk_id = hashlib.sha256(f"{file_path}{0}{len(content)}".encode()).hexdigest()
        vector = list(self.embedding_model.embed([content]))[0].tolist()

        data = [{
            "id": chunk_id,
            "file_path": str(file_path),
            "symbol_name": "file_root",
            "content": content,
            "start_line": 0,
            "end_line": len(content.splitlines()),
            "vector": vector,
        }]

        self.table.add(data)
        return 1

    def search_semantic(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        query_vector = list(self.embedding_model.embed([query]))[0].tolist()
        return self.table.search(query_vector).limit(limit).to_list()