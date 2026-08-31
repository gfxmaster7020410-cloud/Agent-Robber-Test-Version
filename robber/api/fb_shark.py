from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from datetime import datetime
import json
import os
from typing import List, Optional

# --- Router Setup ---
router = APIRouter(prefix="/api/fb_shark", tags=["FB SHARK Engine"])

# --- Configuration & Storage ---
CONFIG_FILE = "data/fb_shark_config.json"

class EngineStatus(BaseModel):
    instances: int
    motion_engine: str
    micro_delay: str
    health: str
    is_running: bool

class AutomationProfile(BaseModel):
    id: str
    target: str
    state: str  # "Executing" | "Idle" | "Cooldown"
    proxy: str

class LogEntry(BaseModel):
    timestamp: str
    message: str
    level: str

# Helper to get worker pool lazily
def _get_worker_pool():
    import server
    return server.worker_pool

# Default data
def get_default_config():
    return {
        "status": {"instances": 0, "motion_engine": "Calibration", "micro_delay": "45ms - 180ms", "health": "Ready", "is_running": False},
        "profiles": [
            {"id": "FB-001", "target": "Social", "state": "Idle", "proxy": "192.168.1.5"},
            {"id": "FB-002", "target": "Web", "state": "Idle", "proxy": "192.168.1.6"},
            {"id": "FB-003", "target": "Social", "state": "Idle", "proxy": "192.168.1.7"}
        ],
        "logs": []
    }

def load_config():
    if not os.path.exists(CONFIG_FILE):
        data = get_default_config()
        save_config(data)
        return data
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

def add_log(message: str, level: str = "INFO"):
    config = load_config()
    log = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "message": message, "level": level}
    config["logs"].append(log)
    save_config(config)

@router.get("/status", response_model=EngineStatus)
async def get_status():
    return load_config()["status"]

@router.get("/profiles", response_model=List[AutomationProfile])
async def get_profiles():
    return load_config()["profiles"]

@router.get("/logs", response_model=List[LogEntry])
async def get_logs():
    return load_config()["logs"][-50:] # Return last 50

@router.post("/start")
async def start_automation():
    config = load_config()
    config["status"].update({"is_running": True, "health": "Optimal", "instances": len(config["profiles"])})
    save_config(config)
    add_log("[SYSTEM] Automation loop initiated")
    
    wp = _get_worker_pool()
    for profile in config["profiles"]:
        await wp.start_worker(profile["id"], profile["target"])
        
    return {"status": "success"}

@router.post("/stop")
async def stop_automation():
    config = load_config()
    config["status"].update({"is_running": False, "health": "Stopped", "instances": 0})
    save_config(config)
    add_log("[ALERT] Emergency stop executed", "ERROR")
    
    wp = _get_worker_pool()
    for profile in config["profiles"]:
        await wp.stop_worker(profile["id"])
        
    return {"status": "success"}

@router.post("/profile/{profile_id}/action")
async def profile_action(profile_id: str, action: str):
    config = load_config()
    for p in config["profiles"]:
        if p["id"] == profile_id:
            p["state"] = action.capitalize()
            save_config(config)
            add_log(f"[PROFILE:{profile_id}] Action {action} triggered")
            return {"status": "success"}
    raise HTTPException(status_code=404, detail="Profile not found")
