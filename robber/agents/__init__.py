"""Sub-agent swarm personas coordinated through SSOT task_state."""

from robber.agents.sub_agents import (
    ROLE_LABELS,
    AuditorWorker,
    ArchitectWorker,
    CodeWorker,
    TestWorker,
    WorkerAgent,
    WorkerResult,
    role_for_task,
)
from robber.agents.pr_writer import PRWriterWorker
from robber.agents.swarm_orchestrator import SwarmOrchestrator, SwarmReport, wants_swarm

__all__ = [
    "ArchitectWorker",
    "AuditorWorker",
    "CodeWorker",
    "PRWriterWorker",
    "ROLE_LABELS",
    "SwarmOrchestrator",
    "SwarmReport",
    "TestWorker",
    "WorkerAgent",
    "WorkerResult",
    "role_for_task",
    "wants_swarm",
]
