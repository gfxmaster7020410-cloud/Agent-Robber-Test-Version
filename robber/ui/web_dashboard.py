"""Lightweight Web Dashboard for Agent Robber real-time telemetry.

FastAPI + WebSocket server that must boot BEFORE model routing or agent loop
execution begins. Streams live system metrics (RAM usage, model routes, active
DAG tasks, token consumption) to connected dashboard clients.

Target: < 80 MB RAM overhead. No heavy frontend build step.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robber.config import (
    COMBINED_RSS_LIMIT,
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    DASHBOARD_WS_INTERVAL,
    PERSONA_ROUTE_MAP,
)
from robber.jail import workspace_root
from robber.ssot_db import get_ssot_db
from robber.telemetry import MemoryGuard, rss_bytes, sample_pids

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

_dashboard_app: Any = None
_connected_clients: set[WebSocket] = set()
_dashboard_task: asyncio.Task | None = None
_broadcast_lock = threading.Lock()

# Module-level app for uvicorn subprocess import
app: Any = None


def _create_app() -> Any:
    if not HAS_FASTAPI:
        raise RuntimeError("fastapi/uvicorn not installed")

    app = FastAPI(title="Agent Robber Dashboard", docs_url=None, redoc_url=None)

    static_dir = Path(__file__).parent / "dashboard_static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        index_file = static_dir / "index.html"
        if index_file.is_file():
            return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
        return HTMLResponse(content="<h1>Agent Robber Dashboard</h1><p>Dashboard loading...</p>")

    @app.get("/api/metrics")
    async def metrics():
        return JSONResponse(content=_collect_metrics())

    @app.get("/api/config")
    async def config():
        return JSONResponse(content=_collect_config())

    @app.get("/api/routes")
    async def routes():
        return JSONResponse(content=_collect_routes())

    @app.websocket("/ws/dashboard")
    async def dashboard_ws(ws: WebSocket):
        await ws.accept()
        _connected_clients.add(ws)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(ws.receive_text(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    break
                else:
                    try:
                        msg = json.loads(data)
                    except Exception:
                        msg = {}
                    if msg.get("type") == "answer":
                        qid = str(msg.get("question_id") or msg.get("id") or "")
                        ans = str(msg.get("answer") or msg.get("text") or "")
                        if qid and ans:
                            from robber.ui.question_engine import get_question_engine
                            get_question_engine().answer(qid, ans)
                metrics_payload = _collect_metrics()
                try:
                    await ws.send_json(metrics_payload)
                except Exception:
                    break
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            _connected_clients.discard(ws)

    @app.websocket("/ws/answer")
    async def answer_ws(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                qid = str(msg.get("question_id") or msg.get("id") or "")
                answer_text = str(msg.get("answer") or msg.get("text") or "")
                if qid and answer_text:
                    from robber.ui.question_engine import get_question_engine
                    accepted = get_question_engine().answer(qid, answer_text)
                    try:
                        await ws.send_json({"ok": accepted, "question_id": qid})
                    except Exception:
                        break
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    return app


def _collect_metrics() -> dict[str, Any]:
    pids = _get_active_pids()
    ram = sample_pids(pids) if pids else {"pids": [], "combined": 0, "limit": COMBINED_RSS_LIMIT}

    tasks = _get_task_counts()
    agents = _get_agent_states()
    token_usage = _get_token_usage()
    budget = _get_budget_status()
    dispatch_log = _get_dispatch_log()
    changes = _get_recent_changes()

    return {
        "timestamp": time.time(),
        "ram": {
            "combined_bytes": ram.get("combined", 0),
            "combined_mb": round(ram.get("combined", 0) / (1024 * 1024), 1),
            "limit_bytes": ram.get("limit", COMBINED_RSS_LIMIT),
            "limit_mb": round(ram.get("limit", COMBINED_RSS_LIMIT) / (1024 * 1024), 1),
            "pct": round(ram.get("combined", 0) / max(1, ram.get("limit", 1)) * 100, 1),
            "pids": ram.get("pids", []),
        },
        "tasks": tasks,
        "agents": agents,
        "token_usage": token_usage,
        "budget": budget,
        "dispatch_log": dispatch_log,
        "recent_changes": changes,
    }


def _collect_config() -> dict[str, Any]:
    from robber.config import PERSONA_ROUTE_MAP, COST_DAILY_LIMIT_USD, COST_MONTHLY_LIMIT_USD
    from robber.vault import providers_status

    return {
        "persona_route_map": PERSONA_ROUTE_MAP,
        "budget": {
            "daily_limit_usd": COST_DAILY_LIMIT_USD,
            "monthly_limit_usd": COST_MONTHLY_LIMIT_USD,
        },
        "providers": providers_status(),
        "ram_limit_bytes": COMBINED_RSS_LIMIT,
    }


def _collect_routes() -> dict[str, Any]:
    from robber.router.session import MODEL_ROUTES, MODEL_META, STRENGTH_LABELS
    return {
        "model_routes": {k: [list(pair) for pair in v] for k, v in MODEL_ROUTES.items()},
        "strength_labels": {k: list(v) for k, v in STRENGTH_LABELS.items()},
        "persona_map": PERSONA_ROUTE_MAP,
    }


def _get_active_pids() -> list[int]:
    try:
        import robber.launcher
        if hasattr(robber.launcher, '_active_stack'):
            stack = robber.launcher._active_stack
            if stack:
                return stack.pids()
    except Exception:
        pass
    return [os.getpid()]


def _get_task_counts() -> dict[str, Any]:
    try:
        db = get_ssot_db()
        with db.connection() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as n FROM task_state GROUP BY status"
            ).fetchall()
        counts = {str(row[0]): int(row[1]) for row in rows}
        total = sum(counts.values())
        return {
            "total": total,
            "by_status": counts,
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "completed": counts.get("completed", 0),
            "failed": counts.get("failed", 0),
        }
    except Exception:
        return {"total": 0, "by_status": {}, "queued": 0, "running": 0, "completed": 0, "failed": 0}


def _get_agent_states() -> list[dict[str, Any]]:
    try:
        db = get_ssot_db()
        agents = db.list_agents(limit=20)
        return [
            {
                "id": str(a.get("id", ""))[:8],
                "worker_id": a.get("worker_id", ""),
                "phase": a.get("phase", ""),
                "loop_status": a.get("loop_status", ""),
                "updated_at": a.get("updated_at", 0),
            }
            for a in agents
        ]
    except Exception:
        return []


def _get_token_usage() -> dict[str, Any]:
    try:
        db = get_ssot_db()
        now = time.time()
        day_start = now - 86400
        daily = db.sum_token_usage(since=day_start)
        monthly = db.sum_token_usage(since=now - 30 * 86400)
        by_provider = {}
        rows = db.list_token_usage(since=day_start, limit=5000)
        for row in rows:
            prov = str(row.get("provider") or "unknown")
            if prov not in by_provider:
                by_provider[prov] = {"input": 0, "output": 0, "cost": 0.0, "count": 0}
            by_provider[prov]["input"] += int(row.get("input_tokens") or 0)
            by_provider[prov]["output"] += int(row.get("output_tokens") or 0)
            by_provider[prov]["cost"] += float(row.get("estimated_cost_usd") or 0.0)
            by_provider[prov]["count"] += 1
        for entry in by_provider.values():
            entry["cost"] = round(entry["cost"], 6)
        return {
            "daily": {
                "input_tokens": daily.get("input_tokens", 0),
                "output_tokens": daily.get("output_tokens", 0),
                "cost_usd": round(daily.get("cost_usd", 0.0), 6),
                "requests": daily.get("count", 0),
            },
            "monthly": {
                "input_tokens": monthly.get("input_tokens", 0),
                "output_tokens": monthly.get("output_tokens", 0),
                "cost_usd": round(monthly.get("cost_usd", 0.0), 6),
                "requests": monthly.get("count", 0),
            },
            "by_provider": by_provider,
        }
    except Exception:
        return {"daily": {}, "monthly": {}, "by_provider": {}}


def _get_budget_status() -> dict[str, Any]:
    try:
        from robber.proxy.rate_limiter import CostAwareRateLimiter
        limiter = CostAwareRateLimiter()
        return limiter.budget_status().public()
    except Exception:
        return {
            "daily_used": 0.0, "daily_limit": 2.0, "daily_pct": 0.0,
            "monthly_used": 0.0, "monthly_limit": 30.0, "monthly_pct": 0.0,
            "warning": False, "should_fallback": False,
        }


def _get_dispatch_log() -> list[dict[str, Any]]:
    try:
        from robber.proxy.router import _global_dispatch_log
        return _global_dispatch_log[-20:]
    except Exception:
        return []


def _get_recent_changes() -> list[dict[str, Any]]:
    try:
        db = get_ssot_db()
        changes = db.list_changes(after=0, limit=10)
        return [
            {"seq": c.get("seq"), "table": c.get("table_name"), "op": c.get("op"), "ts": c.get("ts")}
            for c in changes
        ]
    except Exception:
        return []


async def _broadcast_loop() -> None:
    while True:
        await asyncio.sleep(DASHBOARD_WS_INTERVAL)
        if not _connected_clients:
            continue
        payload = _collect_metrics()
        dead: list[WebSocket] = []
        for ws in list(_connected_clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            _connected_clients.discard(ws)


def launch_web_dashboard(port: int = DASHBOARD_PORT, host: str = DASHBOARD_HOST) -> bool:
    """Start the dashboard server in a background thread. Returns True on success."""
    if not HAS_FASTAPI:
        logger.error("[dashboard] fastapi/uvicorn not installed; dashboard skipped")
        return False

    global _dashboard_app
    _dashboard_app = _create_app()

    def _run() -> None:
        try:
            config = uvicorn.Config(
                _dashboard_app,
                host=host,
                port=port,
                log_level="warning",
                access_log=False,
                loop="asyncio",
            )
            server = uvicorn.Server(config)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def _start_broadcast():
                global _dashboard_task
                _dashboard_task = loop.create_task(_broadcast_loop())

            loop.run_until_complete(_start_broadcast())
            loop.run_until_complete(server.serve())
        except Exception as exc:
            logger.error("[dashboard] server failed: %s", exc)

    thread = threading.Thread(target=_run, name="robber-dashboard", daemon=True)
    thread.start()

    deadline = time.time() + 8.0
    while time.time() < deadline:
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://{host}:{port}/", timeout=1.0) as resp:
                if resp.status < 500:
                    logger.info("[dashboard] bound http://%s:%d", host, port)
                    return True
        except Exception:
            time.sleep(0.2)

    logger.warning("[dashboard] port %d did not respond within 8s", port)
    return False


def get_dashboard_app() -> Any:
    global _dashboard_app
    if _dashboard_app is None:
        _dashboard_app = _create_app()
    return _dashboard_app


def broadcast_dashboard_event(event_type: str, data: dict[str, Any]) -> None:
    payload = json.dumps({"type": event_type, **data})
    with _broadcast_lock:
        dead: list[WebSocket] = []
        for ws in list(_connected_clients):
            try:
                asyncio.get_event_loop().run_until_complete(
                    ws.send_text(payload)
                )
            except Exception:
                dead.append(ws)
        for ws in dead:
            _connected_clients.discard(ws)


def main() -> int:
    global app
    port = int(os.environ.get("DASHBOARD_PORT", str(DASHBOARD_PORT)))
    host = os.environ.get("DASHBOARD_HOST", DASHBOARD_HOST)
    app = _create_app()
    _dashboard_app_inner = app
    ok = launch_web_dashboard(port=port, host=host)
    if not ok:
        return 1
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
