from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any


class AgentTaskStatus(StrEnum):
    CREATING = "creating"
    READY = "ready"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CLEANUP_PENDING = "cleanup_pending"
    ARCHIVED = "archived"
    ORPHANED = "orphaned"


_TRANSITIONS: dict[AgentTaskStatus, frozenset[AgentTaskStatus]] = {
    AgentTaskStatus.CREATING: frozenset({AgentTaskStatus.READY, AgentTaskStatus.FAILED, AgentTaskStatus.CANCELLED}),
    AgentTaskStatus.READY: frozenset({AgentTaskStatus.RUNNING, AgentTaskStatus.FAILED, AgentTaskStatus.CANCELLED}),
    AgentTaskStatus.RUNNING: frozenset(
        {
            AgentTaskStatus.WAITING_APPROVAL,
            AgentTaskStatus.COMPLETED,
            AgentTaskStatus.FAILED,
            AgentTaskStatus.CANCELLED,
        }
    ),
    AgentTaskStatus.WAITING_APPROVAL: frozenset(
        {AgentTaskStatus.RUNNING, AgentTaskStatus.COMPLETED, AgentTaskStatus.FAILED, AgentTaskStatus.CANCELLED}
    ),
    AgentTaskStatus.COMPLETED: frozenset({AgentTaskStatus.CLEANUP_PENDING, AgentTaskStatus.ARCHIVED}),
    AgentTaskStatus.FAILED: frozenset({AgentTaskStatus.READY, AgentTaskStatus.CLEANUP_PENDING, AgentTaskStatus.ARCHIVED}),
    AgentTaskStatus.CANCELLED: frozenset({AgentTaskStatus.CLEANUP_PENDING, AgentTaskStatus.ARCHIVED}),
    AgentTaskStatus.CLEANUP_PENDING: frozenset({AgentTaskStatus.ARCHIVED, AgentTaskStatus.FAILED}),
    AgentTaskStatus.ARCHIVED: frozenset(),
    AgentTaskStatus.ORPHANED: frozenset({AgentTaskStatus.FAILED, AgentTaskStatus.CLEANUP_PENDING, AgentTaskStatus.ARCHIVED}),
}


@dataclass
class AgentTask:
    """Client-owned metadata for one isolated Agent execution.

    The App Server remains the source of truth for conversation bodies.  This
    object intentionally stores only the stable association and lifecycle
    metadata needed to restore and route a task.
    """

    id: str
    project_id: str
    title: str
    task_prompt: str
    provider_id: str
    model_id: str | None
    reasoning_effort: str | None
    thread_id: str | None
    worktree_path: Path
    branch_name: str
    status: AgentTaskStatus = AgentTaskStatus.CREATING
    created_at: str = field(default_factory=lambda: _now())
    updated_at: str = field(default_factory=lambda: _now())
    completion_metadata: dict[str, Any] = field(default_factory=dict)
    active_turn_id: str | None = None

    @property
    def is_running(self) -> bool:
        return self.status in {AgentTaskStatus.RUNNING, AgentTaskStatus.WAITING_APPROVAL}

    @property
    def status_label(self) -> str:
        return {
            AgentTaskStatus.CREATING: "Creating",
            AgentTaskStatus.READY: "Ready",
            AgentTaskStatus.RUNNING: "Running",
            AgentTaskStatus.WAITING_APPROVAL: "Waiting Approval",
            AgentTaskStatus.COMPLETED: "Completed",
            AgentTaskStatus.FAILED: "Failed",
            AgentTaskStatus.CANCELLED: "Cancelled",
            AgentTaskStatus.CLEANUP_PENDING: "Cleanup pending",
            AgentTaskStatus.ARCHIVED: "Archived",
            AgentTaskStatus.ORPHANED: "Orphaned Worktree",
        }[self.status]

    @property
    def status_icon(self) -> str:
        return {
            AgentTaskStatus.CREATING: "◌",
            AgentTaskStatus.READY: "●",
            AgentTaskStatus.RUNNING: "◉",
            AgentTaskStatus.WAITING_APPROVAL: "⚠",
            AgentTaskStatus.COMPLETED: "✓",
            AgentTaskStatus.FAILED: "×",
            AgentTaskStatus.CANCELLED: "■",
            AgentTaskStatus.CLEANUP_PENDING: "!",
            AgentTaskStatus.ARCHIVED: "—",
            AgentTaskStatus.ORPHANED: "⚠",
        }[self.status]

    def transition_to(self, status: AgentTaskStatus, **metadata: Any) -> None:
        status = AgentTaskStatus(status)
        if status != self.status and status not in _TRANSITIONS[self.status]:
            raise ValueError(f"Invalid Agent Task transition: {self.status.value} -> {status.value}")
        self.status = status
        if metadata:
            self.completion_metadata = {**self.completion_metadata, **metadata}
        self.updated_at = _now()

    def recover_to_ready(self, **metadata: Any) -> None:
        """Recover an interrupted in-memory execution without replaying it."""

        if self.status in {AgentTaskStatus.RUNNING, AgentTaskStatus.WAITING_APPROVAL, AgentTaskStatus.CREATING}:
            self.status = AgentTaskStatus.READY
        if metadata:
            self.completion_metadata = {**self.completion_metadata, **metadata}
        self.active_turn_id = None
        self.updated_at = _now()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
