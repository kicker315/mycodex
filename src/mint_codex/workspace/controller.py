from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Signal

from mint_codex.core.provider_router import ProviderRouter
from mint_codex.models.session import Thread, ThreadSummary

from .registry import Project, ProjectRegistry
from .state import WorkspaceState


class WorkspaceController(ProviderRouter):
    """Project-scoped desktop workspace over the existing ProviderRouter.

    ``state.active_project_id`` is the only authoritative project selection.
    Project objects, the UI, the thread index, and the provider cwd are
    projections of that id and are never independent selections.
    """

    projects_changed = Signal(object)
    current_project_changed = Signal(object)
    workspace_state_changed = Signal(object)
    workspace_threads_changed = Signal(object)
    workspace_loading_changed = Signal(bool)

    def __init__(
        self,
        cwd: Path | None = None,
        parent=None,
        registry: ProjectRegistry | None = None,
        **router_kwargs,
    ):
        self.registry = registry or ProjectRegistry()
        projects = self.registry.list_projects()
        saved_active_id = self.registry.get_active_project_id()
        initial = self.registry.get_project(saved_active_id) if saved_active_id else None
        initial = initial or (projects[0] if projects else None)
        super().__init__(cwd=(initial.path if initial else cwd), parent=parent, **router_kwargs)
        self.state = WorkspaceState()
        self._pending_resume: tuple[str, str] | None = None
        self._history_loading = False
        self.thread_history_changed.connect(self._on_router_history)
        self.thread_changed.connect(self._on_router_thread)

        if initial is not None:
            self.activate_project(initial.id, source="restore")

    @property
    def active_project_id(self) -> str | None:
        return self.state.active_project_id

    @property
    def projects(self) -> list[Project]:
        return self.registry.list_projects()

    @property
    def active_project(self) -> Project | None:
        project_id = self.state.active_project_id
        return self.registry.get_project(project_id) if project_id else None

    @property
    def current_project(self) -> Project | None:
        """Compatibility alias; always derived from ``active_project_id``."""

        return self.active_project

    @property
    def workspace_threads(self) -> list[ThreadSummary]:
        project_id = self.state.active_project_id
        return self.registry.list_threads(project_id) if project_id else []

    @property
    def history_loading(self) -> bool:
        return self._history_loading

    def add_project(self, path: Path, name: str | None = None) -> Project:
        project = self.registry.add_project(
            path,
            name=name,
            default_provider=self.selected_provider,
            default_model=self._selected_models.get(self.selected_provider),
        )
        self.activate_project(project.id, source="add-project")
        return project

    def refresh_projects(self) -> list[Project]:
        projects = self.projects
        self.projects_changed.emit(projects)
        self.workspace_state_changed.emit(self.state)
        return projects

    def activate_project(
        self,
        project_id: str,
        *,
        source: str = "unknown",
        resume_key: tuple[str, str] | None = None,
    ) -> Project | None:
        """Atomically switch the workspace to the stable Project id."""

        project = self.registry.get_project(project_id)
        if project is None:
            self._fail(f"Unknown Project: {project_id}")
            return None

        # Commit identity before any signal or App Server operation so every
        # synchronous observer sees the same project id while the transition
        # is in progress.
        project = self.registry.open_project(project_id)
        self.registry.set_active_project_id(project.id)
        self.state.active_project_id = project.id
        self.state.draft_thread_project_id = None
        self._pending_resume = resume_key if resume_key is not None else self.registry.last_thread(project.id)
        self._set_history_loading(True)
        super().set_workspace_path(project.path)

        projects = self.projects
        active = self.active_project
        self.projects_changed.emit(projects)
        self.current_project_changed.emit(active)
        self.workspace_threads_changed.emit(self.workspace_threads)
        self.workspace_state_changed.emit(self.state)
        return active

    def open_project(self, project_id: str) -> Project | None:
        """Compatibility entry point; all callers converge on activation."""

        return self.activate_project(project_id, source="open-project")

    def set_workspace_path(self, path: Path) -> None:
        """Keep the inherited operational cwd derived from the active Project."""

        project = self.active_project
        resolved = path.expanduser().resolve()
        if project is not None and resolved != project.path:
            self._fail("Workspace/project cwd mismatch; Project activation is required")
            return
        super().set_workspace_path(resolved)

    def remove_project(self, project_id: str) -> None:
        was_active = self.active_project_id == project_id
        self.registry.remove_project(project_id)
        self.projects_changed.emit(self.projects)
        if not was_active:
            self.workspace_state_changed.emit(self.state)
            return

        next_project = next(iter(self.projects), None)
        if next_project is not None:
            self.activate_project(next_project.id, source="remove-project-fallback")
            return

        self.state.active_project_id = None
        self.registry.set_active_project_id(None)
        self._pending_resume = None
        self._set_history_loading(False)
        super().set_workspace_path(Path.cwd())
        self.current_project_changed.emit(None)
        self.workspace_threads_changed.emit([])
        self.workspace_state_changed.emit(self.state)

    def set_project_pinned(self, project_id: str, pinned: bool) -> None:
        self.registry.set_pinned(project_id, pinned)
        self.refresh_projects()

    def threads_for_project(self, project_id: str) -> list[ThreadSummary]:
        if self.registry.get_project(project_id) is None:
            return []
        return self.registry.list_threads(project_id)

    def new_thread_for_project(self, project_id: str) -> bool:
        if self.registry.get_project(project_id) is None:
            self._fail(f"Unknown Project: {project_id}")
            return False
        if self.active_project_id != project_id:
            if self.activate_project(project_id, source="project-new-thread") is None:
                return False
        return self.new_thread()

    def new_thread(self) -> bool:
        if self.active_project is None:
            self.error.emit("Add or open a Project before starting a Thread")
            return False
        if not self._workspace_consistent("new Thread"):
            return False
        super().new_thread()
        self.registry.set_last_thread(self.active_project.id, None, None)
        self.state.draft_thread_project_id = self.active_project.id
        self.workspace_state_changed.emit(self.state)
        return True

    def send_text(self, text: str) -> None:
        if self.active_project is None:
            self.error.emit("Add or open a Project before sending a message")
            return
        if not self._workspace_consistent("send Turn"):
            return
        super().send_text(text)

    def resume_thread(self, thread: ThreadSummary | tuple[str, str] | str, provider: str | None = None) -> None:
        summary = self._resolve_summary(thread, provider)
        if summary is None:
            return

        target_id = summary.project_id
        if target_id is None and summary.cwd:
            project = self.registry.get_project_by_path(Path(summary.cwd))
            target_id = project.id if project else None
        if target_id is None:
            self._fail(summary.provider, "Thread is not bound to a Project")
            return
        if self.registry.get_project(target_id) is None:
            self._fail(summary.provider, f"Thread references unknown Project: {target_id}")
            return

        if target_id != self.active_project_id:
            self.activate_project(
                target_id,
                source="resume-thread",
                resume_key=(summary.id, summary.provider),
            )
            # If the provider is already ready, route immediately. Otherwise
            # the pending key is consumed by the next thread/list response.
            backend = self.backends.get(summary.provider)
            if backend is not None and backend.is_ready:
                self._pending_resume = None
                self._resume_summary(summary)
            return

        self._resume_summary(summary)

    def refresh_history(self) -> None:
        self._set_history_loading(True)
        super().refresh_history()

    def _resume_summary(self, summary: ThreadSummary) -> None:
        if self.active_project_id != summary.project_id:
            self._fail(summary.provider, "Thread/Project state mismatch; resume rejected")
            return
        if not self._workspace_consistent("resume Thread", summary):
            return
        super().resume_thread(summary)

    def _workspace_consistent(self, operation: str, summary: ThreadSummary | None = None) -> bool:
        project = self.active_project
        if project is None or self.active_project_id is None:
            self._fail(f"Cannot {operation}: no active Project")
            return False
        if self.cwd.expanduser().resolve() != project.path:
            self._fail(f"Cannot {operation}: workspace/project cwd mismatch")
            return False
        thread = self.current_thread
        if thread is not None:
            if thread.project_id != project.id:
                self._fail(f"Cannot {operation}: current Thread/Project mismatch")
                return False
            if thread.cwd and Path(thread.cwd).expanduser().resolve() != project.path:
                self._fail(f"Cannot {operation}: current Thread cwd mismatch")
                return False
        if summary is not None and summary.cwd and Path(summary.cwd).expanduser().resolve() != project.path:
            self._fail(f"Cannot {operation}: Thread cwd mismatch")
            return False
        return True

    def _on_router_history(self, summaries: object) -> None:
        self._set_history_loading(False)
        project = self.active_project
        if project is None:
            self.workspace_threads_changed.emit([])
            return
        existing = {
            (summary.provider, summary.id): summary
            for summary in self.registry.list_threads(project.id)
        }
        normalized = [
            replace(
                summary,
                title=summary.title or existing.get((summary.provider, summary.id), summary).title,
                project_id=project.id,
            )
            for summary in summaries
            if isinstance(summary, ThreadSummary)
            and (not summary.cwd or Path(summary.cwd).expanduser().resolve() == project.path)
        ]
        self.registry.replace_threads(project.id, normalized)
        self.workspace_threads_changed.emit(normalized)
        if self._pending_resume is None:
            return
        for summary in normalized:
            if (summary.id, summary.provider) == self._pending_resume:
                self._pending_resume = None
                self._resume_summary(summary)
                break

    def _set_history_loading(self, loading: bool) -> None:
        self._history_loading = loading
        self.workspace_loading_changed.emit(loading)

    def _on_router_thread(self, thread: object) -> None:
        project = self.active_project
        if project is None:
            return
        if isinstance(thread, Thread):
            self.state.draft_thread_project_id = None
            thread.project_id = project.id
            self.registry.set_last_thread(project.id, thread.id, thread.provider)
            self.registry.upsert_thread(
                project.id,
                ThreadSummary(
                    thread.id,
                    thread.provider,
                    thread.model,
                    thread.title,
                    thread.cwd,
                    None,
                    project.id,
                ),
            )
            self.workspace_threads_changed.emit(self.registry.list_threads(project.id))
            self.workspace_state_changed.emit(self.state)
