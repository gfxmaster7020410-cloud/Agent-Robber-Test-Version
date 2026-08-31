import json
import os
from typing import List, Dict, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/memo", tags=["MeMO Store"])
STORE_FILE = "data/memo_store.json"

class MemoryChunk(BaseModel):
    id: str
    content: str
    raw: str
    tag: str
    tokens: int
    date: str
    metadata: Dict[str, Any]

def load_data() -> List[dict]:
    if not os.path.exists(STORE_FILE):
        return []
    try:
        with open(STORE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

def save_data(data: List[dict]):
    os.makedirs(os.path.dirname(STORE_FILE), exist_ok=True)
    with open(STORE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

@router.get("/list")
def list_memo():
    return load_data()

@router.post("/add")
def add_memo(chunk: MemoryChunk):
    data = load_data()
    data.append(chunk.dict())
    save_data(data)
    return chunk

@router.put("/update/{chunk_id}")
def update_memo(chunk_id: str, chunk: MemoryChunk):
    data = load_data()
    for i, item in enumerate(data):
        if item["id"] == chunk_id:
            data[i] = chunk.dict()
            save_data(data)
            return data[i]
    raise HTTPException(status_code=404, detail="Chunk not found")

@router.delete("/delete/{chunk_id}")
def delete_memo(chunk_id: str):
    data = load_data()
    new_data = [item for item in data if item["id"] != chunk_id]
    if len(new_data) == len(data):
        raise HTTPException(status_code=404, detail="Chunk not found")
    save_data(new_data)
    return {"message": "Deleted"}

@router.post("/purge")
def purge_memo():
    save_data([])
    return {"message": "Store purged"}
