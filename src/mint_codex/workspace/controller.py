from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

from PySide6.QtCore import Signal

from mint_codex.core.provider_router import ProviderRouter
from mint_codex.models.agent_task import AgentTask, AgentTaskStatus
from mint_codex.models.session import Item, Thread, ThreadSummary, Turn
from mint_codex.models.timeline import ApprovalRequest

from .registry import Project, ProjectRegistry
from .state import WorkspaceContext, WorkspaceContextKind, WorkspaceState
from .worktrees import WorktreeError, WorktreeManager

log = logging.getLogger(__name__)


@dataclass
class _AgentRuntime:
    task: AgentTask
    thread: Thread | None = None
    turns: dict[str, Turn] = field(default_factory=dict)
    items: dict[str, Item] = field(default_factory=dict)
    pending_approvals: dict[str, ApprovalRequest] = field(default_factory=dict)
    cancel_requested: bool = False


class WorkspaceController(ProviderRouter):
    """Project-scoped desktop workspace over the existing ProviderRouter.

    ``state.active_project_id`` and ``state.active_workspace_context`` are the
    authoritative selections. Project objects, the UI, Git, Terminal, and
    provider cwd are projections of those ids and are never independent
    selections.
    """

    projects_changed = Signal(object)
    current_project_changed = Signal(object)
    workspace_state_changed = Signal(object)
    workspace_threads_changed = Signal(object)
    workspace_loading_changed = Signal(bool)
    agent_tasks_changed = Signal(object)
    agent_task_changed = Signal(object)
    workspace_context_changed = Signal(object)
    agent_task_selected = Signal(object, object)
    agent_task_timeline_event = Signal(str, str, object)
    agent_task_approval_requested = Signal(object)

    def __init__(
        self,
        cwd: Path | None = None,
        parent=None,
        registry: ProjectRegistry | None = None,
        **router_kwargs,
    ):
        self.registry = registry or ProjectRegistry()
        managed_worktrees = router_kwargs.pop("worktree_root", None)
        self.worktree_manager = router_kwargs.pop("worktree_manager", None) or WorktreeManager(managed_worktrees)
        self.max_running_agent_tasks = int(router_kwargs.pop("max_running_agent_tasks", 3))
        if self.max_running_agent_tasks < 1:
            raise ValueError("max_running_agent_tasks must be positive")
        projects = self.registry.list_projects()
        saved_active_id = self.registry.get_active_project_id()
        initial = self.registry.get_project(saved_active_id) if saved_active_id else None
        initial = initial or (projects[0] if projects else None)
        super().__init__(cwd=(initial.path if initial else cwd), parent=parent, **router_kwargs)
        self.state = WorkspaceState()
        self._agent_tasks: dict[str, AgentTask] = {}
        self._agent_runtimes: dict[str, _AgentRuntime] = {}
        self._agent_task_by_thread: dict[tuple[str, str], str] = {}
        self._pending_agent_starts: set[str] = set()
        self._pending_agent_recovery: set[str] = set()
        self._load_agent_tasks()
        saved_task_id = self.registry.get_active_workspace_task_id()
        self._pending_resume: tuple[str, str] | None = None
        self._history_loading = False
        self.thread_history_changed.connect(self._on_router_history)
        self.thread_changed.connect(self._on_router_thread)

        if initial is not None:
            self.activate_project(initial.id, source="restore")
            if saved_task_id is not None:
                saved_task = self._agent_tasks.get(saved_task_id)
                if saved_task is not None and saved_task.project_id == initial.id:
                    self.activate_agent_task(saved_task.id, source="restore")

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
    def active_workspace_context(self) -> WorkspaceContext | None:
        return self.state.active_workspace_context

    @property
    def active_workspace_root(self) -> Path | None:
        context = self.active_workspace_context
        if context is None:
            return None
        if context.kind == WorkspaceContextKind.MAIN_WORKSPACE:
            project = self.registry.get_project(context.project_id)
            return project.path if project else None
        task = self._agent_tasks.get(context.task_id or "")
        return task.worktree_path if task else None

    @property
    def active_agent_task(self) -> AgentTask | None:
        context = self.active_workspace_context
        if context is None or context.kind != WorkspaceContextKind.AGENT_TASK:
            return None
        return self._agent_tasks.get(context.task_id or "")

    @property
    def agent_tasks(self) -> list[AgentTask]:
        project_id = self.state.active_project_id
        return [task for task in self._agent_tasks.values() if task.project_id == project_id and task.status != AgentTaskStatus.ARCHIVED]

    def agent_tasks_for_project(self, project_id: str) -> list[AgentTask]:
        return [task for task in self._agent_tasks.values() if task.project_id == project_id and task.status != AgentTaskStatus.ARCHIVED]

    def agent_task(self, task_id: str) -> AgentTask | None:
        return self._agent_tasks.get(task_id)

    @property
    def current_thread(self) -> Thread | None:
        task = self.active_agent_task
        if task is not None:
            runtime = self._agent_runtimes.get(task.id)
            return runtime.thread if runtime else None
        return super().current_thread

    @property
    def thread(self) -> Thread | None:
        return self.current_thread

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
        self.registry.set_active_workspace_task_id(None)
        self.state.active_project_id = project.id
        self.state.draft_thread_project_id = None
        self.state.active_workspace_context = WorkspaceContext(project.id)
        self._pending_resume = resume_key if resume_key is not None else self.registry.last_thread(project.id)
        self._set_history_loading(True)
        super().set_workspace_path(project.path)

        projects = self.projects
        active = self.active_project
        self.projects_changed.emit(projects)
        self.current_project_changed.emit(active)
        self.workspace_threads_changed.emit(self.workspace_threads)
        self.workspace_state_changed.emit(self.state)
        self.workspace_context_changed.emit(self.state.active_workspace_context)
        return active

    def activate_main_workspace(self, *, source: str = "main-workspace") -> bool:
        project = self.active_project
        if project is None:
            self.error.emit("Add or open a Project before selecting its Main Workspace")
            return False
        if self.active_workspace_context == WorkspaceContext(project.id):
            return True
        self.registry.set_active_workspace_task_id(None)
        self.state.active_workspace_context = WorkspaceContext(project.id)
        super().set_workspace_path(project.path)
        self.workspace_context_changed.emit(self.state.active_workspace_context)
        self.workspace_state_changed.emit(self.state)
        return True

    def activate_agent_task(self, task_id: str, *, source: str = "agent-task") -> bool:
        task = self._agent_tasks.get(task_id)
        if task is None:
            self._fail(f"Unknown Agent Task: {task_id}")
            return False
        project = self.registry.get_project(task.project_id)
        if project is None:
            self._fail(f"Agent Task references unknown Project: {task.project_id}")
            return False
        if self.active_project_id != project.id:
            if self.activate_project(project.id, source=source) is None:
                return False
        inspection = self.worktree_manager.inspect(project.path, project.id, task.id, task.branch_name)
        if not inspection.exists:
            self._set_task_status(task, AgentTaskStatus.ORPHANED, error="Missing Worktree")
            self.error.emit(f"Agent Task {task.title}: worktree is missing")
        elif not inspection.usable:
            self._set_task_status(task, AgentTaskStatus.FAILED, error=inspection.error or "Worktree is unavailable")
            return False
        self.registry.set_active_workspace_task_id(task.id)
        self.state.active_workspace_context = WorkspaceContext(project.id, WorkspaceContextKind.AGENT_TASK, task.id)
        self.cwd = task.worktree_path.expanduser().resolve(strict=False)
        self._workspace_generation += 1
        self._replace_current(None)
        self._set_busy(False)
        runtime = self._agent_runtimes.setdefault(task.id, _AgentRuntime(task))
        self.agent_task_selected.emit(task, runtime.thread)
        self.workspace_context_changed.emit(self.state.active_workspace_context)
        self.workspace_state_changed.emit(self.state)
        if runtime.thread is None and task.thread_id and inspection.usable:
            self._resume_agent_task(task, restore_only=True)
        return True

    def open_project(self, project_id: str) -> Project | None:
        """Compatibility entry point; all callers converge on activation."""

        return self.activate_project(project_id, source="open-project")

    def create_agent_task(
        self,
        title: str,
        task_prompt: str,
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AgentTask | None:
        project = self.active_project
        title = " ".join(title.split())
        task_prompt = task_prompt.strip()
        provider_id = provider_id or self.selected_provider
        model_id = model_id if model_id is not None else self._selected_models.get(provider_id)
        reasoning_effort = reasoning_effort.strip() if isinstance(reasoning_effort, str) else None
        if project is None:
            self.error.emit("Add or open a Project before creating an Agent Task")
            return None
        if not title:
            self.error.emit("Agent Task title cannot be empty")
            return None
        if not task_prompt:
            self.error.emit("Agent Task prompt cannot be empty")
            return None
        if provider_id not in self.backends:
            self.error.emit(f"Unknown Provider: {provider_id}")
            return None
        try:
            self.worktree_manager.validate_repository(project.path)
            task_id = str(uuid.uuid4())
            worktree_path = self.worktree_manager.task_path(project.id, task_id)
            branch_name = self.worktree_manager.branch_name(project.id, task_id)
        except WorktreeError as exc:
            self.error.emit(str(exc))
            return None

        task = AgentTask(
            id=task_id,
            project_id=project.id,
            title=title,
            task_prompt=task_prompt,
            provider_id=provider_id,
            model_id=model_id,
            reasoning_effort=reasoning_effort,
            thread_id=None,
            worktree_path=worktree_path,
            branch_name=branch_name,
        )
        self._agent_tasks[task.id] = task
        self.registry.upsert_agent_task(task)
        self._emit_agent_tasks()
        try:
            self.worktree_manager.create(project.path, project.id, task.id)
            self._set_task_status(task, AgentTaskStatus.READY)
        except WorktreeError as exc:
            self._set_task_status(task, AgentTaskStatus.FAILED, error=str(exc))
            self.error.emit(f"Agent Task worktree creation failed: {exc}")
            return task
        self.start_agent_task(task.id)
        return task

    def start_agent_task(self, task_id: str) -> bool:
        task = self._agent_tasks.get(task_id)
        if task is None:
            self._fail(f"Unknown Agent Task: {task_id}")
            return False
        if task.status not in {AgentTaskStatus.READY, AgentTaskStatus.FAILED}:
            self.error.emit(f"Agent Task cannot start from {task.status_label}")
            return False
        if task.status == AgentTaskStatus.FAILED:
            try:
                task.transition_to(AgentTaskStatus.READY)
            except ValueError:
                self.error.emit("Failed Agent Task cannot be restarted safely")
                return False
            self.registry.upsert_agent_task(task)
        project = self.registry.get_project(task.project_id)
        if project is None:
            self._set_task_status(task, AgentTaskStatus.FAILED, error="Unknown Project")
            return False
        inspection = self.worktree_manager.inspect(project.path, project.id, task.id, task.branch_name)
        if not inspection.usable:
            self._set_task_status(task, AgentTaskStatus.FAILED, error=inspection.error or "Worktree unavailable")
            return False
        if self._running_agent_count() >= self.max_running_agent_tasks:
            self._set_task_status(task, AgentTaskStatus.READY, error="Concurrency limit reached; start manually later")
            self.error.emit(f"Agent Task limit reached ({self.max_running_agent_tasks}); Task remains Ready")
            return False
        backend = self.backends.get(task.provider_id)
        if backend is None:
            self._set_task_status(task, AgentTaskStatus.FAILED, error="Provider backend unavailable")
            return False
        self.activate_agent_task(task.id, source="start-agent-task")
        if not backend.is_ready:
            self._pending_agent_starts.add(task.id)
            if not backend.is_running:
                backend.start()
            return True
        self._begin_agent_thread(task)
        return True

    def cancel_agent_task(self, task_id: str) -> bool:
        task = self._agent_tasks.get(task_id)
        if task is None:
            self._fail(f"Unknown Agent Task: {task_id}")
            return False
        if task.status in {AgentTaskStatus.COMPLETED, AgentTaskStatus.CANCELLED, AgentTaskStatus.ARCHIVED}:
            return False
        runtime = self._agent_runtimes.get(task.id)
        if runtime is not None:
            runtime.cancel_requested = True
            backend = self.backends.get(task.provider_id)
            for request in list(runtime.pending_approvals.values()):
                if backend is not None and backend.is_ready:
                    backend.respond(request.request_id, self._approval_response(request, "cancel"))
                runtime.pending_approvals.pop(request.request_key, None)
            if backend is not None and backend.is_ready and task.thread_id and task.active_turn_id:
                backend.request(
                    "turn/interrupt",
                    {"threadId": task.thread_id, "turnId": task.active_turn_id},
                )
        self._pending_agent_starts.discard(task.id)
        self._pending_agent_recovery.discard(task.id)
        task.active_turn_id = None
        self._set_task_status(task, AgentTaskStatus.CANCELLED, reason="Cancelled by user")
        return True

    def inspect_agent_task(self, task_id: str):
        task = self._agent_tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        project = self.registry.get_project(task.project_id)
        if project is None:
            raise WorktreeError("Agent Task references an unknown Project")
        return self.worktree_manager.inspect(project.path, project.id, task.id, task.branch_name)

    def cleanup_agent_task(self, task_id: str, *, terminal_active: bool = False) -> bool:
        task = self._agent_tasks.get(task_id)
        if task is None:
            self._fail(f"Unknown Agent Task: {task_id}")
            return False
        if task.status in {
            AgentTaskStatus.CREATING,
            AgentTaskStatus.RUNNING,
            AgentTaskStatus.WAITING_APPROVAL,
        } or task.active_turn_id:
            self.error.emit("Cannot cleanup an Agent Task while it is active")
            return False
        if terminal_active:
            self.error.emit("Cannot cleanup an Agent Task with an active Terminal")
            return False
        project = self.registry.get_project(task.project_id)
        if project is None:
            self.error.emit("Cannot cleanup an Agent Task without its Project")
            return False
        try:
            task.transition_to(AgentTaskStatus.CLEANUP_PENDING)
            self.registry.upsert_agent_task(task)
            self.worktree_manager.cleanup(project.path, project.id, task.id, task.branch_name)
            task.transition_to(AgentTaskStatus.ARCHIVED)
            self.registry.upsert_agent_task(task)
        except (WorktreeError, ValueError) as exc:
            if task.status == AgentTaskStatus.CLEANUP_PENDING:
                task.transition_to(AgentTaskStatus.FAILED, error=str(exc))
                self.registry.upsert_agent_task(task)
            self.error.emit(str(exc))
            return False
        self._agent_runtimes.pop(task.id, None)
        self._emit_agent_tasks()
        if self.active_agent_task and self.active_agent_task.id == task.id:
            self.activate_main_workspace(source="cleanup-agent-task")
        return True

    def set_workspace_path(self, path: Path) -> None:
        """Keep the inherited operational cwd derived from the active Project."""

        project = self.active_project
        resolved = path.expanduser().resolve()
        if project is not None and resolved != project.path:
            self._fail("Workspace/project cwd mismatch; Project activation is required")
            return
        super().set_workspace_path(resolved)

    def set_provider_api_key(self, provider_id: str, api_key: str | None) -> bool:
        if any(task.is_running and task.provider_id == provider_id for task in self._agent_tasks.values()):
            config = self.providers.get(provider_id)
            self.error.emit(
                f"Cannot change {(config.display_name if config else provider_id)} API key while an Agent Task is running"
            )
            return False
        return super().set_provider_api_key(provider_id, api_key)

    def remove_project(self, project_id: str) -> None:
        if self.registry.get_project(project_id) is None:
            self._fail(f"Unknown Project: {project_id}")
            return
        active_tasks = self.registry.list_agent_tasks(project_id, include_archived=False)
        if active_tasks:
            self.error.emit("Cleanup or archive all Agent Tasks before removing the Project")
            return
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
        self.registry.set_active_workspace_task_id(None)
        self.state.active_workspace_context = None
        self._pending_resume = None
        self._set_history_loading(False)
        super().set_workspace_path(Path.cwd())
        self.current_project_changed.emit(None)
        self.workspace_threads_changed.emit([])
        self.workspace_state_changed.emit(self.state)
        self.workspace_context_changed.emit(None)

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
        if self.active_workspace_context and self.active_workspace_context.kind == WorkspaceContextKind.AGENT_TASK:
            if not self.activate_main_workspace(source="new-main-thread"):
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
        if self.active_workspace_context and self.active_workspace_context.kind == WorkspaceContextKind.AGENT_TASK:
            self.send_agent_task_text(self.active_workspace_context.task_id or "", text)
            return
        if not self._workspace_consistent("send Turn"):
            return
        super().send_text(text)

    def send_agent_task_text(self, task_id: str, text: str) -> None:
        task = self._agent_tasks.get(task_id)
        text = text.strip()
        if task is None or not text:
            return
        if self.active_agent_task is None or self.active_agent_task.id != task_id:
            if not self.activate_agent_task(task_id, source="send-agent-task"):
                return
        if task.status != AgentTaskStatus.READY or task.thread_id is None:
            self.error.emit(f"Agent Task is {task.status_label}; it cannot accept a new Turn")
            return
        self._start_agent_turn(task, text)

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

        if self.active_workspace_context and self.active_workspace_context.kind == WorkspaceContextKind.AGENT_TASK:
            if not self.activate_main_workspace(source="resume-main-thread"):
                return
        self._resume_summary(summary)

    def refresh_history(self) -> None:
        self._set_history_loading(True)
        project = self.active_project
        task_context = self.active_workspace_context and self.active_workspace_context.kind == WorkspaceContextKind.AGENT_TASK
        saved_cwd = self.cwd
        if task_context and project is not None:
            self.cwd = project.path
        super().refresh_history()
        if task_context:
            self.cwd = saved_cwd

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

    def _load_agent_tasks(self) -> None:
        for project in self.registry.list_projects():
            for task in self.registry.list_agent_tasks(project.id, include_archived=False):
                self._agent_tasks[task.id] = task
                if task.status in {AgentTaskStatus.RUNNING, AgentTaskStatus.WAITING_APPROVAL}:
                    task.recover_to_ready(recovery="Application restarted while Task was active")
                    self.registry.upsert_agent_task(task)
                    if task.thread_id:
                        self._pending_agent_recovery.add(task.id)
                try:
                    inspection = self.worktree_manager.inspect(project.path, project.id, task.id, task.branch_name)
                except WorktreeError as exc:
                    inspection = None
                    task.completion_metadata = {**task.completion_metadata, "recovery_error": str(exc)}
                if inspection is not None and not inspection.exists:
                    task.status = AgentTaskStatus.ORPHANED
                    task.updated_at = _task_now()
                    task.completion_metadata = {**task.completion_metadata, "recovery_error": "Missing Worktree"}
                    self.registry.upsert_agent_task(task)

    def _emit_agent_tasks(self) -> None:
        tasks = self.agent_tasks
        self.agent_tasks_changed.emit(tasks)

    def _set_task_status(self, task: AgentTask, status: AgentTaskStatus, **metadata: object) -> None:
        try:
            task.transition_to(status, **metadata)
        except ValueError:
            # Recovery and failure reporting must never hide a real error behind
            # an invalid persisted state.  Keep the state explicit and durable.
            task.status = status
            task.updated_at = _task_now()
            if metadata:
                task.completion_metadata = {**task.completion_metadata, **metadata}
        self.registry.upsert_agent_task(task)
        self.agent_task_changed.emit(task)
        self._emit_agent_tasks()

    def _running_agent_count(self) -> int:
        return sum(1 for task in self._agent_tasks.values() if task.is_running)

    def _begin_agent_thread(self, task: AgentTask) -> None:
        project = self.registry.get_project(task.project_id)
        backend = self.backends.get(task.provider_id)
        if project is None or backend is None or not backend.is_ready:
            return
        runtime = self._agent_runtimes.setdefault(task.id, _AgentRuntime(task))
        if task.thread_id:
            if runtime.thread is None:
                self._resume_agent_task(task, restore_only=False)
            elif task.status == AgentTaskStatus.READY:
                self._start_agent_turn(task, task.task_prompt)
            return
        params: dict[str, object] = {
            "cwd": str(task.worktree_path),
            "modelProvider": task.provider_id,
        }
        if task.model_id:
            params["model"] = task.model_id
        backend.request(
            "thread/start",
            params,
            lambda result, error, task_id=task.id: self._agent_thread_started(task_id, result, error),
        )

    def _agent_thread_started(self, task_id: str, result: object, error: dict | None) -> None:
        task = self._agent_tasks.get(task_id)
        if task is None:
            return
        if error or not isinstance(result, dict):
            self._set_task_status(task, AgentTaskStatus.FAILED, error=(error or {}).get("message", "invalid thread/start response"))
            return
        data = result.get("thread", {})
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            self._set_task_status(task, AgentTaskStatus.FAILED, error="thread/start response has no thread id")
            return
        reported_cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else result.get("cwd")
        if reported_cwd and Path(reported_cwd).expanduser().resolve() != task.worktree_path.resolve():
            self._set_task_status(task, AgentTaskStatus.FAILED, error="Thread cwd does not match Task worktree")
            return
        reported_provider = data.get("modelProvider")
        if reported_provider and reported_provider != task.provider_id:
            self._set_task_status(task, AgentTaskStatus.FAILED, error="Thread provider does not match Task provider")
            return
        thread = Thread(
            id=data["id"],
            provider=task.provider_id,
            model=result.get("model") if isinstance(result.get("model"), str) else task.model_id,
            title=task.title,
            cwd=str(task.worktree_path),
            reasoning_effort=result.get("reasoningEffort") if isinstance(result.get("reasoningEffort"), str) else task.reasoning_effort,
        )
        runtime = self._agent_runtimes.setdefault(task.id, _AgentRuntime(task))
        runtime.thread = thread
        task.thread_id = thread.id
        task.model_id = thread.model
        task.reasoning_effort = thread.reasoning_effort
        self._agent_task_by_thread[(task.provider_id, thread.id)] = task.id
        self.registry.upsert_agent_task(task)
        self.agent_task_changed.emit(task)
        self._emit_agent_tasks()
        if task.status != AgentTaskStatus.CANCELLED:
            self._start_agent_turn(task, task.task_prompt)

    def _start_agent_turn(self, task: AgentTask, text: str) -> None:
        runtime = self._agent_runtimes.setdefault(task.id, _AgentRuntime(task))
        backend = self.backends.get(task.provider_id)
        if backend is None or not backend.is_ready or not task.thread_id:
            self._set_task_status(task, AgentTaskStatus.FAILED, error="Agent Task App Server is not ready")
            return
        if task.status == AgentTaskStatus.READY:
            self._set_task_status(task, AgentTaskStatus.RUNNING)
        runtime.cancel_requested = False
        self._task_timeline(task.id, "user", {"text": text})
        params: dict[str, object] = {
            "threadId": task.thread_id,
            "input": [{"type": "text", "text": text}],
            "cwd": str(task.worktree_path),
        }
        if task.model_id:
            params["model"] = task.model_id
        if task.reasoning_effort:
            params["effort"] = task.reasoning_effort
        backend.request(
            "turn/start",
            params,
            lambda result, error, task_id=task.id: self._agent_turn_accepted(task_id, result, error),
        )

    def _agent_turn_accepted(self, task_id: str, result: object, error: dict | None) -> None:
        task = self._agent_tasks.get(task_id)
        if task is None:
            return
        if error:
            self._set_task_status(task, AgentTaskStatus.FAILED, error=error.get("message", error))
            self._task_timeline(task.id, "turn_failed", {"status": "failed", "message": error.get("message", error)})
            return
        if isinstance(result, dict):
            turn_data = result.get("turn", {})
            if isinstance(turn_data, dict) and isinstance(turn_data.get("id"), str):
                task.active_turn_id = turn_data["id"]
                self.registry.upsert_agent_task(task)
                runtime = self._agent_runtimes.get(task.id)
                if task.status == AgentTaskStatus.CANCELLED or (runtime is not None and runtime.cancel_requested):
                    backend = self.backends.get(task.provider_id)
                    if backend is not None and backend.is_ready and task.thread_id:
                        backend.request(
                            "turn/interrupt",
                            {"threadId": task.thread_id, "turnId": task.active_turn_id},
                        )

    def _resume_agent_task(self, task: AgentTask, *, restore_only: bool) -> None:
        backend = self.backends.get(task.provider_id)
        if backend is None or not backend.is_ready or not task.thread_id:
            if backend is not None and not backend.is_running:
                self._pending_agent_recovery.add(task.id)
                backend.start()
            return
        backend.request(
            "thread/resume",
            {"threadId": task.thread_id, "cwd": str(task.worktree_path), "modelProvider": task.provider_id},
            lambda result, error, task_id=task.id, view_only=restore_only: self._agent_thread_resumed(task_id, result, error, view_only),
        )

    def _agent_thread_resumed(self, task_id: str, result: object, error: dict | None, view_only: bool) -> None:
        task = self._agent_tasks.get(task_id)
        if task is None:
            return
        if error or not isinstance(result, dict):
            self._set_task_status(task, AgentTaskStatus.FAILED, error=(error or {}).get("message", "thread/resume failed"))
            return
        data = result.get("thread", {})
        if not isinstance(data, dict) or data.get("id") != task.thread_id:
            self._set_task_status(task, AgentTaskStatus.FAILED, error="thread/resume returned an unexpected Thread")
            return
        reported_cwd = result.get("cwd") if isinstance(result.get("cwd"), str) else data.get("cwd")
        if reported_cwd and Path(reported_cwd).expanduser().resolve() != task.worktree_path.resolve():
            self._set_task_status(task, AgentTaskStatus.FAILED, error="Resumed Thread cwd does not match Task worktree")
            return
        runtime = self._agent_runtimes.setdefault(task.id, _AgentRuntime(task))
        runtime.thread = self._task_thread_from_data(task, data, result)
        self._agent_task_by_thread[(task.provider_id, task.thread_id or "")] = task.id
        self._pending_agent_recovery.discard(task.id)
        if not view_only and task.status == AgentTaskStatus.READY:
            self._start_agent_turn(task, task.task_prompt)
        if self.active_agent_task and self.active_agent_task.id == task.id:
            self.agent_task_selected.emit(task, runtime.thread)

    def _task_thread_from_data(self, task: AgentTask, data: dict, response: dict) -> Thread:
        thread = Thread(
            task.thread_id or data["id"],
            provider=task.provider_id,
            model=response.get("model") if isinstance(response.get("model"), str) else task.model_id,
            title=task.title,
            cwd=str(task.worktree_path),
            reasoning_effort=response.get("reasoningEffort") if isinstance(response.get("reasoningEffort"), str) else task.reasoning_effort,
        )
        runtime = self._agent_runtimes.setdefault(task.id, _AgentRuntime(task))
        runtime.turns.clear()
        runtime.items.clear()
        turns = data.get("turns", []) if isinstance(data.get("turns"), list) else []
        for turn_data in turns:
            if not isinstance(turn_data, dict) or not isinstance(turn_data.get("id"), str):
                continue
            turn = Turn(turn_data["id"], status=turn_data.get("status", "completed"))
            runtime.turns[turn.id] = turn
            thread.turns.append(turn)
            items = turn_data.get("items", []) if isinstance(turn_data.get("items"), list) else []
            for item_data in items:
                if not isinstance(item_data, dict) or not isinstance(item_data.get("id"), str):
                    continue
                item = Item(item_data["id"], item_data.get("type", "unknown"), item_data)
                item.completed = True
                item.text = self._item_text(item_data)
                turn.items.append(item)
                runtime.items[item.id] = item
        return thread

    def _task_id_for_thread(self, provider_id: str, params: dict) -> str | None:
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            return None
        return self._agent_task_by_thread.get((provider_id, thread_id))

    def _on_backend_notification(self, provider_id: str, method: str, params: object) -> None:
        if isinstance(params, dict):
            task_id = self._task_id_for_thread(provider_id, params)
            if task_id is not None:
                self._handle_agent_notification(task_id, provider_id, method, params)
                return
        super()._on_backend_notification(provider_id, method, params)

    def _handle_agent_notification(self, task_id: str, provider_id: str, method: str, params: dict) -> None:
        task = self._agent_tasks.get(task_id)
        runtime = self._agent_runtimes.get(task_id)
        if task is None or runtime is None:
            return
        if method == "turn/started":
            data = params.get("turn", {})
            if isinstance(data, dict) and isinstance(data.get("id"), str):
                turn = runtime.turns.setdefault(
                    data["id"], Turn(data["id"], status=data.get("status", "inProgress"))
                )
                if runtime.thread is not None and turn not in runtime.thread.turns:
                    runtime.thread.turns.append(turn)
                task.active_turn_id = turn.id
                if task.status != AgentTaskStatus.CANCELLED and not runtime.cancel_requested:
                    self._set_task_status(task, AgentTaskStatus.RUNNING)
                else:
                    backend = self.backends.get(provider_id)
                    if backend is not None and backend.is_ready and task.thread_id:
                        backend.request(
                            "turn/interrupt",
                            {"threadId": task.thread_id, "turnId": turn.id},
                        )
            self._task_timeline(task_id, "turn_started", data)
        elif method == "item/started":
            data = params.get("item", {})
            if not isinstance(data, dict) or not isinstance(data.get("id"), str):
                return
            item = Item(data["id"], data.get("type", "unknown"), data)
            runtime.items[item.id] = item
            turn = runtime.turns.get(params.get("turnId"))
            if turn is not None:
                turn.items.append(item)
            self._task_timeline(task_id, "item_started", data)
        elif method == "item/agentMessage/delta":
            item_id, delta = params.get("itemId"), params.get("delta")
            if not isinstance(item_id, str) or not isinstance(delta, str):
                return
            item = runtime.items.get(item_id)
            if item is None:
                item = Item(item_id, "agentMessage", {"id": item_id, "type": "agentMessage", "text": ""})
                runtime.items[item_id] = item
                turn = runtime.turns.get(params.get("turnId"))
                if turn is not None:
                    turn.items.append(item)
            item.text += delta
            self._task_timeline(task_id, "agent_delta", params)
        elif method == "item/commandExecution/outputDelta":
            item_id, delta = params.get("itemId"), params.get("delta")
            if not isinstance(item_id, str) or not isinstance(delta, str):
                return
            item = runtime.items.get(item_id)
            if item is not None:
                item.text += delta
            self._task_timeline(task_id, "command_output_delta", params)
        elif method == "item/fileChange/outputDelta":
            if isinstance(params.get("itemId"), str) and isinstance(params.get("delta"), str):
                self._task_timeline(task_id, "file_change_output_delta", params)
        elif method == "item/fileChange/patchUpdated":
            item_id, changes = params.get("itemId"), params.get("changes")
            if not isinstance(item_id, str) or not isinstance(changes, list):
                return
            item = runtime.items.get(item_id)
            if item is not None:
                item.data = {**item.data, "changes": changes}
            self._task_timeline(task_id, "file_change_updated", params)
        elif method == "item/mcpToolCall/progress":
            if isinstance(params.get("itemId"), str) and isinstance(params.get("message"), str):
                self._task_timeline(task_id, "tool_progress", params)
        elif method == "item/completed":
            data = params.get("item", {})
            if not isinstance(data, dict):
                return
            item = runtime.items.get(data.get("id"))
            if item is not None:
                item.data = data
                item.completed = True
            self._task_timeline(task_id, "item_completed", data)
        elif method == "turn/completed":
            data = params.get("turn", {})
            if not isinstance(data, dict):
                return
            turn = runtime.turns.get(data.get("id"))
            status = data.get("status", "completed")
            if turn is not None:
                turn.status = status
            task.active_turn_id = None
            if runtime.cancel_requested or task.status == AgentTaskStatus.CANCELLED or status == "interrupted":
                if runtime.cancel_requested or task.status == AgentTaskStatus.CANCELLED:
                    self._set_task_status(task, AgentTaskStatus.CANCELLED, turn_status=status)
                else:
                    self._set_task_status(task, AgentTaskStatus.FAILED, error="Turn interrupted", turn_status=status)
            elif status == "completed":
                self._set_task_status(task, AgentTaskStatus.COMPLETED, turn_status=status)
            else:
                details = data.get("error") or data.get("lastError") or "Codex did not complete this turn"
                message = details.get("message", details) if isinstance(details, dict) else str(details)
                self._set_task_status(task, AgentTaskStatus.FAILED, error=message, turn_status=status)
            self.registry.upsert_agent_task(task)
            self._task_timeline(task_id, "turn_completed", data)
        elif method == "error":
            details = params.get("error", params)
            message = details.get("message", details) if isinstance(details, dict) else str(details)
            if not params.get("willRetry"):
                self._set_task_status(task, AgentTaskStatus.FAILED, error=message)
            self._task_timeline(task_id, "error", {"message": message})

    def _on_backend_server_request(self, provider_id: str, request_id: object, method: str, params: object) -> None:
        if isinstance(params, dict):
            task_id = self._task_id_for_thread(provider_id, params)
            if task_id is not None:
                self._handle_agent_server_request(task_id, provider_id, request_id, method, params)
                return
        super()._on_backend_server_request(provider_id, request_id, method, params)

    def _handle_agent_server_request(self, task_id: str, provider_id: str, request_id: object, method: str, params: dict) -> None:
        backend = self.backends[provider_id]
        supported = {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "applyPatchApproval",
            "execCommandApproval",
        }
        if method not in supported:
            backend.respond_error(request_id, -32601, f"Unsupported client handler: {method}")
            self._task_timeline(task_id, "unsupported_request", {"provider": provider_id, "method": method})
            return
        request = self._approval_request(provider_id, request_id, method, params)
        if request is None or request.thread_id != self._agent_tasks[task_id].thread_id:
            backend.respond_error(request_id, -32602, "Malformed or mismatched Agent Task approval request")
            return
        request = replace(request, task_id=task_id)
        runtime = self._agent_runtimes[task_id]
        if request.request_key in runtime.pending_approvals:
            backend.respond_error(request_id, -4091, "Duplicate approval request")
            return
        runtime.pending_approvals[request.request_key] = request
        task = self._agent_tasks[task_id]
        self._set_task_status(task, AgentTaskStatus.WAITING_APPROVAL)
        self.agent_task_approval_requested.emit(request)
        self._task_timeline(task_id, "approval_requested", {"request": request})

    def respond_to_approval(self, request_key: str, decision: str) -> bool:
        for runtime in self._agent_runtimes.values():
            request = runtime.pending_approvals.get(request_key)
            if request is None:
                continue
            if decision not in request.allowed_decisions:
                self._fail(f"Unsupported approval decision: {decision}")
                return False
            backend = self.backends.get(request.provider_id)
            if backend is None or not backend.is_ready:
                self._fail(request.provider_id, "Approval App Server is not running")
                return False
            backend.respond(request.request_id, self._approval_response(request, decision))
            runtime.pending_approvals.pop(request_key, None)
            task = self._agent_tasks[request.task_id or ""]
            self._set_task_status(task, AgentTaskStatus.RUNNING)
            self.approval_resolved.emit({"request": request, "decision": decision})
            self._task_timeline(task.id, "approval_resolved", {"request": request, "decision": decision})
            return True
        return super().respond_to_approval(request_key, decision)

    def _on_backend_ready(self, provider_id: str) -> None:
        # ProviderRouter's history refresh is for the Main Workspace.  Keep it
        # on the Project root even while the active UI context is a task
        # worktree; task history is resumed by its own thread id below.
        task_context = self.active_workspace_context and self.active_workspace_context.kind == WorkspaceContextKind.AGENT_TASK
        project = self.active_project
        saved_cwd = self.cwd
        if task_context and project is not None:
            self.cwd = project.path
        super()._on_backend_ready(provider_id)
        if task_context:
            self.cwd = saved_cwd
        for task_id in list(self._pending_agent_starts):
            task = self._agent_tasks.get(task_id)
            if task is not None and task.provider_id == provider_id:
                self._pending_agent_starts.discard(task_id)
                self._begin_agent_thread(task)
        for task_id in list(self._pending_agent_recovery):
            task = self._agent_tasks.get(task_id)
            if task is not None and task.provider_id == provider_id:
                self._resume_agent_task(task, restore_only=True)

    def _task_timeline(self, task_id: str, kind: str, data: object) -> None:
        self.agent_task_timeline_event.emit(task_id, kind, data)

    def _pending_task_approvals(self, task_id: str) -> list[ApprovalRequest]:
        runtime = self._agent_runtimes.get(task_id)
        return list(runtime.pending_approvals.values()) if runtime else []

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


def _task_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
