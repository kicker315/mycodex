from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WorkspaceContextKind(StrEnum):
    MAIN_WORKSPACE = "main_workspace"
    AGENT_TASK = "agent_task"


@dataclass(frozen=True)
class WorkspaceContext:
    project_id: str
    kind: WorkspaceContextKind = WorkspaceContextKind.MAIN_WORKSPACE
    task_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind == WorkspaceContextKind.MAIN_WORKSPACE and self.task_id is not None:
            raise ValueError("Main workspace context cannot carry a task id")
        if self.kind == WorkspaceContextKind.AGENT_TASK and not self.task_id:
            raise ValueError("Agent task workspace context requires a task id")

    @property
    def key(self) -> str:
        return f"main:{self.project_id}" if self.kind == WorkspaceContextKind.MAIN_WORKSPACE else f"task:{self.task_id}"


@dataclass
class WorkspaceState:
    """Authoritative client-owned workspace selection state."""

    active_project_id: str | None = None
    draft_thread_project_id: str | None = None
    active_workspace_context: WorkspaceContext | None = None
