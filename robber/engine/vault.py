import os
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import Page

class SnapshotVaultManager:
    def __init__(self, hub):
        self.hub = hub
        self.base_path = Path("data/vault_snapshots")
        self.base_path.mkdir(parents=True, exist_ok=True)

    async def capture_failure_snapshot(self, page: Page, profile_id: str, error_type: str, details: str):
        timestamp = int(time.time())
        iso_timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        profile_dir = self.base_path / profile_id
        profile_dir.mkdir(parents=True, exist_ok=True)
        
        filename_base = f"{timestamp}_{error_type}"
        image_path = profile_dir / f"{filename_base}.png"
        json_path = profile_dir / f"{filename_base}.json"
        
        # Save screenshot
        await page.screenshot(path=str(image_path), full_page=True)
        
        # Save JSON sidecar
        sidecar = {
            "url": page.url,
            "timestamp": iso_timestamp,
            "error_type": error_type,
            "details": details,
            "profile_id": profile_id
        }
        json_path.write_text(json.dumps(sidecar, indent=2))
        
        # Notify WebSocket
        await self.hub.broadcast({
            "type": "vault_alert",
            "content": "Snapshot captured due to execution error",
            "metadata": {
                "profile_id": profile_id,
                "error_type": error_type,
                "image_path": str(image_path),
                "timestamp": iso_timestamp
            },
            "timestamp": time.time(),
        })
        return image_path
