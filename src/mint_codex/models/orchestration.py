from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class OrchestrationRunStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    APPROVED = "approved"
    RUNNING = "running"
    PAUSED = "paused"
    RECOVERY_REQUIRED = "recovery_required"
    COMPLETED = "completed"
    PARTIAL_FAILED = "partial_failed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class OrchestrationNodeStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


_RUN_TRANSITIONS: dict[OrchestrationRunStatus, frozenset[OrchestrationRunStatus]] = {
    OrchestrationRunStatus.DRAFT: frozenset(
        {OrchestrationRunStatus.VALIDATED, OrchestrationRunStatus.CANCELLED, OrchestrationRunStatus.FAILED}
    ),
    OrchestrationRunStatus.VALIDATED: frozenset(
        {OrchestrationRunStatus.DRAFT, OrchestrationRunStatus.APPROVED, OrchestrationRunStatus.CANCELLED}
    ),
    OrchestrationRunStatus.APPROVED: frozenset(
        {OrchestrationRunStatus.RUNNING, OrchestrationRunStatus.CANCELLED, OrchestrationRunStatus.FAILED}
    ),
    OrchestrationRunStatus.RUNNING: frozenset(
        {
            OrchestrationRunStatus.PAUSED,
            OrchestrationRunStatus.RECOVERY_REQUIRED,
            OrchestrationRunStatus.COMPLETED,
            OrchestrationRunStatus.PARTIAL_FAILED,
            OrchestrationRunStatus.CANCELLED,
            OrchestrationRunStatus.FAILED,
        }
    ),
    OrchestrationRunStatus.PAUSED: frozenset(
        {
            OrchestrationRunStatus.RUNNING,
            OrchestrationRunStatus.RECOVERY_REQUIRED,
            OrchestrationRunStatus.COMPLETED,
            OrchestrationRunStatus.PARTIAL_FAILED,
            OrchestrationRunStatus.CANCELLED,
        }
    ),
    OrchestrationRunStatus.RECOVERY_REQUIRED: frozenset(
        {
            OrchestrationRunStatus.PAUSED,
            OrchestrationRunStatus.RUNNING,
            OrchestrationRunStatus.COMPLETED,
            OrchestrationRunStatus.PARTIAL_FAILED,
            OrchestrationRunStatus.FAILED,
            OrchestrationRunStatus.CANCELLED,
        }
    ),
    OrchestrationRunStatus.COMPLETED: frozenset(),
    OrchestrationRunStatus.PARTIAL_FAILED: frozenset(),
    OrchestrationRunStatus.CANCELLED: frozenset(),
    OrchestrationRunStatus.FAILED: frozenset(),
}

_NODE_TRANSITIONS: dict[OrchestrationNodeStatus, frozenset[OrchestrationNodeStatus]] = {
    OrchestrationNodeStatus.PENDING: frozenset(
        {
            OrchestrationNodeStatus.READY,
            OrchestrationNodeStatus.BLOCKED,
            OrchestrationNodeStatus.CANCELLED,
            OrchestrationNodeStatus.FAILED,
        }
    ),
    OrchestrationNodeStatus.READY: frozenset(
        {
            OrchestrationNodeStatus.STARTING,
            OrchestrationNodeStatus.BLOCKED,
            OrchestrationNodeStatus.CANCELLED,
            OrchestrationNodeStatus.FAILED,
        }
    ),
    OrchestrationNodeStatus.STARTING: frozenset(
        {
            OrchestrationNodeStatus.RUNNING,
            OrchestrationNodeStatus.SUCCEEDED,
            OrchestrationNodeStatus.FAILED,
            OrchestrationNodeStatus.CANCELLED,
        }
    ),
    OrchestrationNodeStatus.RUNNING: frozenset(
        {
            OrchestrationNodeStatus.SUCCEEDED,
            OrchestrationNodeStatus.FAILED,
            OrchestrationNodeStatus.CANCELLED,
        }
    ),
    OrchestrationNodeStatus.SUCCEEDED: frozenset(),
    OrchestrationNodeStatus.FAILED: frozenset(),
    OrchestrationNodeStatus.CANCELLED: frozenset(),
    OrchestrationNodeStatus.BLOCKED: frozenset(),
}


@dataclass(frozen=True)
class ResultEnvelope:
    """Immutable, bounded result metadata shared across RESULT_ONLY edges."""

    node_id: str
    agent_task_id: str
    terminal_status: str
    final_message: str = ""
    changed_files: tuple[str, ...] = ()
    branch_name: str | None = None
    worktree_reference: str | None = None
    error_summary: str | None = None
    started_at: str | None = None
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "agent_task_id": self.agent_task_id,
            "terminal_status": self.terminal_status,
            "final_message": self.final_message,
            "changed_files": list(self.changed_files),
            "branch_name": self.branch_name,
            "worktree_reference": self.worktree_reference,
            "error_summary": self.error_summary,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> ResultEnvelope | None:
        if not isinstance(payload, dict):
            return None
        node_id = payload.get("node_id")
        task_id = payload.get("agent_task_id")
        status = payload.get("terminal_status")
        if not all(isinstance(value, str) and value for value in (node_id, task_id, status)):
            return None
        files = payload.get("changed_files", ())
        changed_files = tuple(value for value in files if isinstance(value, str)) if isinstance(files, (list, tuple)) else ()
        return cls(
            node_id=node_id,
            agent_task_id=task_id,
            terminal_status=status,
            final_message=str(payload.get("final_message") or "")[:12000],
            changed_files=changed_files[:100],
            branch_name=payload.get("branch_name") if isinstance(payload.get("branch_name"), str) else None,
            worktree_reference=payload.get("worktree_reference")
            if isinstance(payload.get("worktree_reference"), str)
            else None,
            error_summary=payload.get("error_summary") if isinstance(payload.get("error_summary"), str) else None,
            started_at=payload.get("started_at") if isinstance(payload.get("started_at"), str) else None,
            completed_at=payload.get("completed_at") if isinstance(payload.get("completed_at"), str) else None,
        )

    def prompt_context(self, max_chars: int = 6000) -> str:
        """Return result-only data without cwd, branch, or local path authority."""

        files = ", ".join(self.changed_files[:50]) or "(none reported)"
        message = self.final_message[: max(0, max_chars - 500)]
        context = (
            f"Upstream node {self.node_id} finished with status {self.terminal_status}.\n"
            f"Final response summary:\n{message}\n"
            f"Changed files summary: {files}\n"
        )
        if self.error_summary:
            context += f"Error summary: {self.error_summary[:1000]}\n"
        return context[:max_chars]


@dataclass
class OrchestrationNode:
    node_id: str
    run_id: str
    title: str
    instructions: str
    dependencies: tuple[str, ...] = ()
    provider_id: str | None = None
    model_id: str | None = None
    capability_hint: str | None = None
    status: OrchestrationNodeStatus = OrchestrationNodeStatus.PENDING
    agent_task_id: str | None = None
    result: ResultEnvelope | None = None
    created_at: str = field(default_factory=lambda: _now())
    updated_at: str = field(default_factory=lambda: _now())
    blocked_reason: str | None = None

    def transition_to(self, status: OrchestrationNodeStatus, **metadata: Any) -> None:
        status = OrchestrationNodeStatus(status)
        if status != self.status and status not in _NODE_TRANSITIONS[self.status]:
            raise ValueError(f"Invalid Orchestration Node transition: {self.status.value} -> {status.value}")
        self.status = status
        if "blocked_reason" in metadata:
            self.blocked_reason = metadata["blocked_reason"]
        if "result" in metadata:
            self.result = metadata["result"]
        self.updated_at = _now()

    def bind_task(self, task_id: str) -> None:
        if self.agent_task_id not in {None, task_id}:
            raise ValueError(f"Orchestration Node {self.node_id} is already bound to an AgentTask")
        self.agent_task_id = task_id
        self.updated_at = _now()


@dataclass
class OrchestrationRun:
    run_id: str
    project_id: str
    objective: str
    base_commit: str
    default_provider_id: str
    default_model_id: str | None
    max_parallelism: int
    plan_version: int = 1
    status: OrchestrationRunStatus = OrchestrationRunStatus.DRAFT
    created_at: str = field(default_factory=lambda: _now())
    approved_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def plan_frozen(self) -> bool:
        return self.status in {
            OrchestrationRunStatus.APPROVED,
            OrchestrationRunStatus.RUNNING,
            OrchestrationRunStatus.PAUSED,
            OrchestrationRunStatus.RECOVERY_REQUIRED,
            OrchestrationRunStatus.COMPLETED,
            OrchestrationRunStatus.PARTIAL_FAILED,
            OrchestrationRunStatus.CANCELLED,
            OrchestrationRunStatus.FAILED,
        }

    def transition_to(self, status: OrchestrationRunStatus, **metadata: Any) -> None:
        status = OrchestrationRunStatus(status)
        if status != self.status and status not in _RUN_TRANSITIONS[self.status]:
            raise ValueError(f"Invalid Orchestration Run transition: {self.status.value} -> {status.value}")
        self.status = status
        now = _now()
        if status == OrchestrationRunStatus.APPROVED:
            self.approved_at = self.approved_at or now
        if status == OrchestrationRunStatus.RUNNING:
            self.started_at = self.started_at or now
        if status in {
            OrchestrationRunStatus.COMPLETED,
            OrchestrationRunStatus.PARTIAL_FAILED,
            OrchestrationRunStatus.CANCELLED,
            OrchestrationRunStatus.FAILED,
        }:
            self.completed_at = self.completed_at or now
        if metadata:
            self.metadata = {**self.metadata, **metadata}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
