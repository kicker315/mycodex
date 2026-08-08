from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WorkspaceState:
    """Authoritative client-owned workspace selection state."""

    active_project_id: str | None = None
    draft_thread_project_id: str | None = None
