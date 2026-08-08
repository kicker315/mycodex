from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from mint_codex.core.providers import default_provider_configs
from mint_codex.models.agent_task import AgentTaskStatus
from mint_codex.workspace.controller import WorkspaceController
from mint_codex.workspace.registry import ProjectRegistry
from mint_codex.workspace.worktrees import DirtyWorktreeError, WorktreeManager


class FakeBackend(QObject):
    connection_changed = Signal(str, str)
    account_changed = Signal(str, str)
    models_changed = Signal(str, object, object)
    notification = Signal(str, str, object)
    server_request = Signal(str, object, str, object)
    ready = Signal(str)
    error = Signal(str, str)

    def __init__(self, provider, _cwd, parent=None):
        super().__init__(parent)
        self.provider = provider
        self._ready = True
        self._running = True
        self.requests: list[tuple[str, object]] = []
        self.responses: list[tuple[object, object]] = []
        self._thread_count = 0
        self._turn_count = 0

    @property
    def is_ready(self):
        return self._ready

    @property
    def is_running(self):
        return self._running

    def start(self):
        self._running = True
        self._ready = True
        self.ready.emit(self.provider.id)

    def stop(self):
        self._running = False
        self._ready = False

    def redact(self, value):
        return value

    def request(self, method, params=None, callback=None):
        self.requests.append((method, params))
        if callback is None:
            return len(self.requests)
        if method == "thread/start":
            self._thread_count += 1
            callback(
                {
                    "model": params.get("model"),
                    "thread": {
                        "id": f"{self.provider.id}-thread-{self._thread_count}",
                        "modelProvider": self.provider.id,
                        "cwd": params["cwd"],
                    },
                },
                None,
            )
        elif method == "thread/resume":
            callback(
                {
                    "thread": {
                        "id": params["threadId"],
                        "modelProvider": self.provider.id,
                        "cwd": params["cwd"],
                        "turns": [],
                    }
                },
                None,
            )
        elif method == "turn/start":
            self._turn_count += 1
            callback({"turn": {"id": f"turn-{self._turn_count}", "status": "inProgress"}}, None)
        return len(self.requests)

    def respond(self, request_id, result):
        self.responses.append((request_id, result))

    def respond_error(self, request_id, code, message):
        self.responses.append((request_id, {"error": {"code": code, "message": message}}))


def _git_project(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "base.py").write_text("BASE = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "base.py"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], check=True)


def _controller(tmp_path: Path, project_path: Path) -> WorkspaceController:
    providers = default_provider_configs(home=tmp_path / "home")
    controller = WorkspaceController(
        cwd=tmp_path,
        registry=ProjectRegistry(tmp_path / "workspace.sqlite3"),
        providers=providers,
        backend_factory=lambda config, cwd, parent: FakeBackend(config, cwd, parent),
        worktree_root=tmp_path / "managed-worktrees",
    )
    controller.add_project(project_path, name="fixture")
    return controller


def test_two_agent_tasks_have_isolated_worktrees_cwds_and_changes(qapp, tmp_path):
    project = tmp_path / "repo"
    _git_project(project)
    controller = _controller(tmp_path, project)

    task_a = controller.create_agent_task("Task A", "Change file A", model_id="gpt-test")
    task_b = controller.create_agent_task("Task B", "Change file B", model_id="gpt-test")

    assert task_a is not None and task_b is not None
    assert task_a.worktree_path != task_b.worktree_path
    assert task_a.branch_name != task_b.branch_name
    assert task_a.status == AgentTaskStatus.RUNNING
    assert task_b.status == AgentTaskStatus.RUNNING
    assert task_a.thread_id != task_b.thread_id
    assert controller.active_workspace_root == task_b.worktree_path
    assert controller.current_thread is not None
    assert controller.current_thread.cwd == str(task_b.worktree_path)
    thread_starts = [params for method, params in controller.backends["openai"].requests if method == "thread/start"]
    assert thread_starts[0]["cwd"] == str(task_a.worktree_path)
    assert thread_starts[1]["cwd"] == str(task_b.worktree_path)

    (task_a.worktree_path / "foo.py").write_text("A\n", encoding="utf-8")
    (task_b.worktree_path / "bar.py").write_text("B\n", encoding="utf-8")
    assert not (project / "foo.py").exists()
    assert not (project / "bar.py").exists()
    assert not (task_b.worktree_path / "foo.py").exists()
    assert not (task_a.worktree_path / "bar.py").exists()


def test_approval_is_routed_by_task_thread_and_cancel_does_not_affect_sibling(qapp, tmp_path):
    project = tmp_path / "repo"
    _git_project(project)
    controller = _controller(tmp_path, project)
    task_a = controller.create_agent_task("Task A", "Run A")
    task_b = controller.create_agent_task("Task B", "Run B")
    assert task_a is not None and task_b is not None
    backend = controller.backends["openai"]
    approvals = []
    controller.agent_task_approval_requested.connect(approvals.append)

    controller._on_backend_server_request(
        "openai",
        101,
        "item/commandExecution/requestApproval",
        {"threadId": task_a.thread_id, "turnId": "turn-a", "itemId": "item-a", "command": "echo A"},
    )
    controller._on_backend_server_request(
        "openai",
        202,
        "item/commandExecution/requestApproval",
        {"threadId": task_b.thread_id, "turnId": "turn-b", "itemId": "item-b", "command": "echo B"},
    )

    assert [request.task_id for request in approvals] == [task_a.id, task_b.id]
    assert task_a.status == AgentTaskStatus.WAITING_APPROVAL
    assert task_b.status == AgentTaskStatus.WAITING_APPROVAL
    assert controller.respond_to_approval(approvals[0].request_key, "accept")
    assert backend.responses[-1] == (101, {"decision": "accept"})
    assert task_a.status == AgentTaskStatus.RUNNING
    assert task_b.status == AgentTaskStatus.WAITING_APPROVAL

    assert controller.cancel_agent_task(task_a.id)
    assert task_a.status == AgentTaskStatus.CANCELLED
    assert task_b.status == AgentTaskStatus.WAITING_APPROVAL
    assert any(method == "turn/interrupt" and params["threadId"] == task_a.thread_id for method, params in backend.requests)


def test_parallel_task_streaming_items_stay_on_their_runtime_turn(qapp, tmp_path):
    project = tmp_path / "repo"
    _git_project(project)
    controller = _controller(tmp_path, project)
    task_a = controller.create_agent_task("Task A", "Stream A")
    task_b = controller.create_agent_task("Task B", "Stream B")
    assert task_a is not None and task_b is not None
    events = []
    controller.agent_task_timeline_event.connect(lambda task_id, kind, data: events.append((task_id, kind, data)))

    controller._on_backend_notification(
        "openai",
        "turn/started",
        {"threadId": task_a.thread_id, "turn": {"id": "stream-a", "status": "inProgress"}},
    )
    controller._on_backend_notification(
        "openai",
        "turn/started",
        {"threadId": task_b.thread_id, "turn": {"id": "stream-b", "status": "inProgress"}},
    )
    controller._on_backend_notification(
        "openai",
        "item/started",
        {"threadId": task_a.thread_id, "turnId": "stream-a", "item": {"id": "item-a", "type": "agentMessage"}},
    )
    controller._on_backend_notification(
        "openai",
        "item/agentMessage/delta",
        {"threadId": task_b.thread_id, "turnId": "stream-b", "itemId": "item-b", "delta": "B"},
    )

    runtime_a = controller._agent_runtimes[task_a.id]
    runtime_b = controller._agent_runtimes[task_b.id]
    assert [item.id for item in runtime_a.turns["stream-a"].items] == ["item-a"]
    assert runtime_b.items["item-b"].text == "B"
    assert {task_id for task_id, _kind, _data in events} == {task_a.id, task_b.id}


def test_cancel_race_cannot_be_overwritten_by_late_turn_events(qapp, tmp_path):
    project = tmp_path / "repo"
    _git_project(project)
    controller = _controller(tmp_path, project)
    task = controller.create_agent_task("Cancel race", "wait")
    assert task is not None
    controller.cancel_agent_task(task.id)
    controller._agent_turn_accepted(task.id, {"turn": {"id": "late-turn", "status": "inProgress"}}, None)
    controller._on_backend_notification(
        "openai",
        "turn/started",
        {"threadId": task.thread_id, "turn": {"id": "late-turn", "status": "inProgress"}},
    )
    controller._on_backend_notification(
        "openai",
        "turn/completed",
        {"threadId": task.thread_id, "turn": {"id": "late-turn", "status": "completed"}},
    )
    assert task.status == AgentTaskStatus.CANCELLED


def test_cleanup_is_safe_for_clean_and_dirty_worktrees(qapp, tmp_path):
    project = tmp_path / "repo"
    _git_project(project)
    controller = _controller(tmp_path, project)
    clean = controller.create_agent_task("Clean", "finish")
    dirty = controller.create_agent_task("Dirty", "finish")
    assert clean is not None and dirty is not None
    controller.cancel_agent_task(clean.id)
    controller.cancel_agent_task(dirty.id)
    (dirty.worktree_path / "uncommitted.txt").write_text("keep me\n", encoding="utf-8")

    assert controller.cleanup_agent_task(clean.id)
    assert clean.status == AgentTaskStatus.ARCHIVED
    assert not clean.worktree_path.exists()
    assert not controller.cleanup_agent_task(dirty.id)
    assert dirty.worktree_path.exists()
    assert (dirty.worktree_path / "uncommitted.txt").exists()
    assert dirty.status == AgentTaskStatus.FAILED


def test_restart_recovers_task_metadata_and_marks_missing_worktree(qapp, tmp_path):
    project = tmp_path / "repo"
    _git_project(project)
    controller = _controller(tmp_path, project)
    task = controller.create_agent_task("Recover me", "inspect")
    assert task is not None
    controller._on_backend_notification(
        "openai",
        "turn/completed",
        {"threadId": task.thread_id, "turn": {"id": task.active_turn_id, "status": "completed"}},
    )
    assert task.status == AgentTaskStatus.COMPLETED
    controller.registry.close()

    restored = _controller(tmp_path, project)
    recovered = restored.agent_task(task.id)
    assert recovered is not None
    assert recovered.status == AgentTaskStatus.COMPLETED
    assert recovered.worktree_path == task.worktree_path
    assert restored.activate_agent_task(task.id)
    assert restored.current_thread is not None
    assert restored.current_thread.cwd == str(task.worktree_path)
    restored.registry.close()

    subprocess.run(["git", "-C", str(project), "worktree", "remove", "--force", str(task.worktree_path)], check=True)
    broken = _controller(tmp_path, project)
    assert broken.agent_task(task.id).status == AgentTaskStatus.ORPHANED


def test_worktree_manager_rejects_path_and_ref_injection(tmp_path):
    manager = WorktreeManager(tmp_path / "managed")
    for project_id, task_id in [("../escape", "task"), ("project", "../escape"), ("project", "a/b")]:
        try:
            manager.task_path(project_id, task_id)
        except Exception as exc:
            assert "Invalid" in str(exc)
        else:
            raise AssertionError("unsafe worktree segment was accepted")
    assert manager.branch_name("project", "task").startswith("mint-codex/project/task")


def test_dirty_worktree_manager_never_force_deletes(tmp_path):
    project = tmp_path / "repo"
    _git_project(project)
    manager = WorktreeManager(tmp_path / "managed")
    path, branch = manager.create(project, "project", "task")
    (path / "dirty.txt").write_text("do not delete\n", encoding="utf-8")
    try:
        manager.cleanup(project, "project", "task", branch)
    except DirtyWorktreeError:
        pass
    else:
        raise AssertionError("dirty worktree was cleaned up")
    assert path.exists()


def test_terminal_contexts_survive_switch_without_crossing_cwds(qapp, tmp_path):
    from mint_codex.ui.developer_workspace import DeveloperWorkspacePanel

    main = tmp_path / "main"
    task = tmp_path / "task"
    main.mkdir()
    task.mkdir()
    panel = DeveloperWorkspacePanel()
    panel.set_project("project", main, "main:project")
    panel.start_terminal()
    main_terminal = panel.terminal
    assert main_terminal is not None
    panel.set_project("project", task, "task:task-1")
    assert panel.terminal is None
    panel.start_terminal()
    task_terminal = panel.terminal
    assert task_terminal is not None
    assert task_terminal is not main_terminal
    assert main_terminal.is_running
    assert task_terminal.root == task.resolve()
    panel.shutdown()
    qapp.processEvents()


def test_main_window_switches_developer_workspace_to_agent_worktree(qapp, tmp_path):
    from PySide6.QtCore import QSettings
    from mint_codex.ui.main_window import MainWindow

    project = tmp_path / "repo"
    _git_project(project)
    controller = _controller(tmp_path, project)
    window = MainWindow(controller, settings=QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
    task = controller.create_agent_task("UI task", "inspect")
    assert task is not None
    controller.activate_agent_task(task.id)
    assert window.developer_workspace.project_root == task.worktree_path.resolve()
    assert controller.active_workspace_root == task.worktree_path
    assert controller.current_thread is not None
    assert controller.current_thread.cwd == str(task.worktree_path)
    window.close()
