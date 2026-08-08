from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mint_codex.core.provider_router import ProviderRouter
from mint_codex.models.agent_task import AgentTask, AgentTaskStatus
from mint_codex.models.orchestration import OrchestrationRun
from mint_codex.models.session import Thread, ThreadSummary
from mint_codex.ui.developer_workspace import DeveloperWorkspacePanel
from mint_codex.ui.orchestration import OrchestrationDialog
from mint_codex.ui.timeline import TimelineWidget
from mint_codex.workspace.controller import WorkspaceController
from mint_codex.workspace.registry import Project
from mint_codex.workspace.state import WorkspaceContextKind


TREE_ITEM_KIND_ROLE = Qt.ItemDataRole.UserRole + 1


class SettingsDialog(QDialog):
    def __init__(self, controller: ProviderRouter, parent: QWidget | None = None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Settings · Providers")
        self.resize(560, 320)
        layout = QVBoxLayout(self)

        providers = QGroupBox("Providers")
        provider_form = QFormLayout(providers)
        self.provider_rows: dict[str, tuple[QLabel, QLabel]] = {}
        for provider_id in controller.provider_ids:
            account = QLabel(controller.provider_account_status(provider_id))
            connection = QLabel(controller.provider_status(provider_id))
            self.provider_rows[provider_id] = account, connection
            provider_form.addRow(f"{controller.providers[provider_id].display_name} account", account)
            provider_form.addRow(f"{controller.providers[provider_id].display_name} connection", connection)
        layout.addWidget(providers)

        deepseek = QGroupBox("DeepSeek")
        deepseek_form = QFormLayout(deepseek)
        key_row = QHBoxLayout()
        self.deepseek_key_input = QLineEdit()
        self.deepseek_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.deepseek_key_input.setPlaceholderText("Enter a key; it is never shown after saving")
        self.save_key = QPushButton("Save Key")
        self.clear_key = QPushButton("Clear")
        key_row.addWidget(self.deepseek_key_input, 1)
        key_row.addWidget(self.save_key)
        key_row.addWidget(self.clear_key)
        self.key_status = QLabel()
        deepseek_form.addRow("API key", key_row)
        deepseek_form.addRow("Key state", self.key_status)
        layout.addWidget(deepseek)
        layout.addStretch(1)

        self.save_key.clicked.connect(self._save_key)
        self.clear_key.clicked.connect(self._clear_key)
        controller.provider_status_changed.connect(self._provider_status_changed)
        controller.provider_account_changed.connect(self._provider_account_changed)
        controller.api_key_status_changed.connect(self._key_status_changed)
        self._key_status_changed("deepseek", controller.provider_api_key_status("deepseek"))

    def _save_key(self) -> None:
        if self.controller.set_provider_api_key("deepseek", self.deepseek_key_input.text()):
            self.deepseek_key_input.clear()

    def _clear_key(self) -> None:
        if self.controller.set_provider_api_key("deepseek", None):
            self.deepseek_key_input.clear()

    def _key_status_changed(self, provider_id: str, status: str) -> None:
        if provider_id != "deepseek":
            return
        labels = {
            "stored": "Saved locally",
            "session": "Session only",
            "environment": "Using DEEPSEEK_API_KEY",
            "missing": "Not configured",
        }
        self.key_status.setText(labels.get(status, "Unknown"))

    def _provider_status_changed(self, provider_id: str, status: str) -> None:
        row = self.provider_rows.get(provider_id)
        if row:
            row[1].setText(status)

    def _provider_account_changed(self, provider_id: str, status: str) -> None:
        row = self.provider_rows.get(provider_id)
        if row:
            row[0].setText(status)


class ProjectTreeRow(QWidget):
    project_clicked = Signal(str)
    new_thread_requested = Signal(str)

    def __init__(self, project: Project, parent: QWidget | None = None):
        super().__init__(parent)
        self.project_id = project.id
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 1, 2, 1)
        layout.setSpacing(4)
        self.name = QLabel(("★ " if project.pinned else "") + project.name)
        self.name.setToolTip(str(project.path))
        self.name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.name, 1)
        self.new_thread_button = QToolButton()
        self.new_thread_button.setText("+")
        self.new_thread_button.setObjectName("new_thread_for_project")
        self.new_thread_button.setToolTip(f"New Thread in {project.name}")
        self.new_thread_button.setAccessibleName(f"New Thread in {project.name}")
        self.new_thread_button.clicked.connect(lambda: self.new_thread_requested.emit(self.project_id))
        layout.addWidget(self.new_thread_button)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.project_clicked.emit(self.project_id)
        super().mousePressEvent(event)


class AgentTaskDialog(QDialog):
    def __init__(self, controller: WorkspaceController, parent: QWidget | None = None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("New Agent Task")
        self.resize(560, 390)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("Short task title")
        self.prompt_input = QTextEdit()
        self.prompt_input.setPlaceholderText("What should this Agent do in its isolated worktree?")
        self.provider_input = QComboBox()
        for provider_id in controller.provider_ids:
            self.provider_input.addItem(controller.providers[provider_id].display_name, provider_id)
        self.model_input = QLineEdit()
        selected_model = controller._selected_models.get(controller.selected_provider)
        self.model_input.setText(selected_model or "")
        self.reasoning_input = QComboBox()
        self.reasoning_input.addItem("Reasoning: default", None)
        for effort in ("low", "medium", "high"):
            self.reasoning_input.addItem(f"Reasoning: {effort}", effort)
        form.addRow("Title", self.title_input)
        form.addRow("Prompt", self.prompt_input)
        form.addRow("Provider", self.provider_input)
        form.addRow("Model", self.model_input)
        form.addRow("Reasoning", self.reasoning_input)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        create = QPushButton("Create Task")
        create.setDefault(True)
        buttons.addWidget(cancel)
        buttons.addWidget(create)
        layout.addLayout(buttons)
        cancel.clicked.connect(self.reject)
        create.clicked.connect(self.accept)

    def values(self) -> tuple[str, str, str, str | None, str | None]:
        return (
            self.title_input.text(),
            self.prompt_input.toPlainText(),
            str(self.provider_input.currentData()),
            self.model_input.text().strip() or None,
            self.reasoning_input.currentData(),
        )


class MainWindow(QMainWindow):
    def __init__(self, controller: WorkspaceController, settings: QSettings | None = None):
        super().__init__()
        self.controller = controller
        self.settings = settings or QSettings("MintCodex", "Desktop")
        self._current_thread_key: tuple[str, str] | None = None
        self._current_task_id: str | None = None
        self._current_run_id: str | None = None
        self._composer_project_id: str | None = None
        expanded = self.settings.value("workspace/expandedProjects", [])
        if isinstance(expanded, str):
            expanded = [expanded]
        self._expanded_project_ids = {value for value in expanded if isinstance(value, str)}
        self.setWindowTitle("Mint Codex Desktop")
        self.resize(1180, 760)
        self._build_ui()
        self._connect_signals()
        self._set_agent_status(controller.agent_status)
        self._restore_ui_state()
        self._set_projects(controller.projects)
        self._set_current_project(controller.current_project)
        if controller.active_agent_task is not None:
            self._set_agent_task(controller.active_agent_task, controller.current_thread)
        else:
            self._set_thread(controller.current_thread)
        self._set_provider(controller.selected_provider)
        self._set_history_loading(controller.history_loading)

    def _build_ui(self) -> None:
        root = QSplitter(Qt.Orientation.Horizontal)
        root.setObjectName("workspace_splitter")
        root.setChildrenCollapsible(False)

        sidebar = QWidget()
        sidebar.setMinimumWidth(220)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(8, 8, 8, 8)

        projects_header = QHBoxLayout()
        projects_header.addWidget(QLabel("PROJECTS"))
        projects_header.addStretch(1)
        self.add_project_button = QToolButton()
        self.add_project_button.setText("+")
        self.remove_project_button = QToolButton()
        self.remove_project_button.setText("−")
        self.pin_project_button = QToolButton()
        self.pin_project_button.setText("☆")
        self.refresh_projects_button = QToolButton()
        self.refresh_projects_button.setText("↻")
        self.refresh_threads_button = QToolButton()
        self.refresh_threads_button.setText("↻")
        self.refresh_threads_button.setToolTip("Refresh Threads")
        projects_header.addWidget(self.add_project_button)
        projects_header.addWidget(self.remove_project_button)
        projects_header.addWidget(self.pin_project_button)
        projects_header.addWidget(self.refresh_projects_button)
        projects_header.addWidget(self.refresh_threads_button)
        sidebar_layout.addLayout(projects_header)
        self.new_agent_task_button = QPushButton("+ New Agent Task")
        self.new_agent_task_button.setObjectName("new_agent_task")
        sidebar_layout.addWidget(self.new_agent_task_button)
        self.new_orchestration_button = QPushButton("+ New Orchestration")
        self.new_orchestration_button.setObjectName("new_orchestration")
        sidebar_layout.addWidget(self.new_orchestration_button)
        self.project_tree = QTreeWidget()
        self.project_tree.setObjectName("project_tree")
        self.project_tree.setHeaderHidden(True)
        self.project_tree.setColumnCount(1)
        self.project_tree.setIndentation(16)
        self.project_tree.setMinimumHeight(180)
        self.project_tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.project_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        sidebar_layout.addWidget(self.project_tree, 1)
        root.addWidget(sidebar)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(10, 8, 10, 8)

        toolbar = QHBoxLayout()
        self.project_name = QLabel("No Project")
        self.project_name.setMinimumWidth(160)
        self.provider = QComboBox()
        for provider_id in self.controller.provider_ids:
            self.provider.addItem(self.controller.providers[provider_id].display_name, provider_id)
        self.models = QComboBox()
        self.models.setMinimumWidth(150)
        self.reasoning = QComboBox()
        self.reasoning.addItem("Reasoning: default", None)
        for effort in ("low", "medium", "high"):
            self.reasoning.addItem(f"Reasoning: {effort}", effort)
        self.agent_status = QLabel("Agent Status: Ready")
        self.connection = self.agent_status  # compatibility alias for existing integrations
        self.thread_name = QLabel("New Thread")
        self.new_thread_toolbar = QPushButton("New Thread")
        self.settings_button = QPushButton("Settings")
        self.cancel_agent_task_button = QPushButton("Cancel Task")
        self.cleanup_agent_task_button = QPushButton("Cleanup Task")
        self.cancel_agent_task_button.setVisible(False)
        self.cleanup_agent_task_button.setVisible(False)
        self.developer_workspace_toggle = QToolButton()
        self.developer_workspace_toggle.setText("Workspace")
        self.developer_workspace_toggle.setCheckable(True)
        self.developer_workspace_toggle.setChecked(True)
        toolbar.addWidget(self.project_name)
        toolbar.addWidget(QLabel("Provider"))
        toolbar.addWidget(self.provider)
        toolbar.addWidget(QLabel("Model"))
        toolbar.addWidget(self.models)
        toolbar.addWidget(self.reasoning)
        toolbar.addWidget(self.agent_status)
        toolbar.addWidget(self.thread_name, 1)
        toolbar.addWidget(self.new_thread_toolbar)
        toolbar.addWidget(self.cancel_agent_task_button)
        toolbar.addWidget(self.cleanup_agent_task_button)
        toolbar.addWidget(self.settings_button)
        toolbar.addWidget(self.developer_workspace_toggle)
        content_layout.addLayout(toolbar)

        self.timeline = TimelineWidget()
        self.timeline.setObjectName("agent_timeline")
        content_layout.addWidget(self.timeline, 1)

        composer = QHBoxLayout()
        self.input = QTextEdit()
        self.input.setPlaceholderText("Ask Codex about this project…")
        self.input.setMaximumHeight(120)
        self.send = QPushButton("Send")
        composer.addWidget(self.input, 1)
        composer.addWidget(self.send)
        content_layout.addLayout(composer)
        root.addWidget(content)
        self.developer_workspace = DeveloperWorkspacePanel()
        self.developer_workspace.setMinimumWidth(300)
        root.addWidget(self.developer_workspace)
        root.setStretchFactor(0, 0)
        root.setStretchFactor(1, 1)
        root.setStretchFactor(2, 0)
        self.setCentralWidget(root)
        self.statusBar().showMessage("Ready")

    def _connect_signals(self) -> None:
        self.project_tree.currentItemChanged.connect(self._tree_item_selected)
        self.project_tree.customContextMenuRequested.connect(self._show_thread_context_menu)
        self.project_tree.itemExpanded.connect(self._project_expanded)
        self.project_tree.itemCollapsed.connect(self._project_collapsed)
        self.add_project_button.clicked.connect(self._add_project)
        self.new_agent_task_button.clicked.connect(self._new_agent_task)
        self.new_orchestration_button.clicked.connect(self._new_orchestration)
        self.remove_project_button.clicked.connect(self._remove_project)
        self.pin_project_button.clicked.connect(self._pin_project)
        self.refresh_projects_button.clicked.connect(self.controller.refresh_projects)
        self.new_thread_toolbar.clicked.connect(self._new_thread)
        self.refresh_threads_button.clicked.connect(self.controller.refresh_history)
        self.settings_button.clicked.connect(self._show_settings)
        self.developer_workspace_toggle.toggled.connect(self._toggle_developer_workspace)
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.models.currentIndexChanged.connect(self._model_changed)
        self.reasoning.currentIndexChanged.connect(self._reasoning_changed)
        self.send.clicked.connect(self._send)

        self.controller.projects_changed.connect(self._set_projects)
        self.controller.current_project_changed.connect(self._set_current_project)
        self.controller.workspace_state_changed.connect(self._sync_workspace_state)
        self.controller.workspace_threads_changed.connect(self._set_threads)
        self.controller.workspace_loading_changed.connect(self._set_history_loading)
        self.controller.agent_status_changed.connect(self._set_agent_status)
        self.controller.models_changed.connect(self._set_models)
        self.controller.provider_changed.connect(self._set_provider)
        self.controller.thread_changed.connect(self._set_thread)
        self.controller.agent_tasks_changed.connect(self._set_agent_tasks)
        self.controller.agent_task_changed.connect(self._agent_task_updated)
        self.controller.agent_task_selected.connect(self._set_agent_task)
        self.controller.agent_task_timeline_event.connect(self._agent_task_timeline_event)
        self.controller.workspace_context_changed.connect(self._workspace_context_changed)
        self.controller.orchestration_runs_changed.connect(self._set_orchestration_runs)
        self.controller.timeline_event.connect(self.timeline.apply_event)
        self.controller.timeline_event.connect(self.developer_workspace.handle_timeline_event)
        self.timeline.file_change_requested.connect(self.developer_workspace.open_changed_file)
        self.controller.approval_requested.connect(self.timeline.add_approval_request)
        self.controller.approval_resolved.connect(self.timeline.resolve_approval)
        self.timeline.approval_decision.connect(self.controller.respond_to_approval)
        self.controller.busy_changed.connect(self._set_busy)
        self.controller.error.connect(self._error)
        self.cancel_agent_task_button.clicked.connect(self._cancel_agent_task)
        self.cleanup_agent_task_button.clicked.connect(self._cleanup_agent_task)

    def _restore_ui_state(self) -> None:
        geometry = self.settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        width = self.settings.value("window/sidebarWidth")
        developer_width = self.settings.value("window/developerPanelWidth")
        visible = self.settings.value("workspace/developerPanelVisible", True)
        if isinstance(visible, str):
            visible = visible.lower() not in {"0", "false", "no"}
        self.developer_workspace_toggle.setChecked(bool(visible))
        self.developer_workspace.setVisible(bool(visible))
        if width or developer_width:
            sidebar_width = int(width or 260)
            right_width = int(developer_width or 360) if visible else 0
            center_width = max(500, self.width() - sidebar_width - right_width)
            self.centralWidget().setSizes([sidebar_width, center_width, right_width])
        tab = self.settings.value("workspace/developerTab")
        if tab is not None:
            self.developer_workspace.tabs.setCurrentIndex(max(0, int(tab)))

    def closeEvent(self, event) -> None:
        splitter = self.centralWidget()
        if isinstance(splitter, QSplitter):
            self.settings.setValue("window/sidebarWidth", splitter.sizes()[0])
            self.settings.setValue("window/developerPanelWidth", splitter.sizes()[2])
        self.settings.setValue("workspace/expandedProjects", sorted(self._expanded_project_ids))
        self.settings.setValue("workspace/developerPanelVisible", self.developer_workspace_toggle.isChecked())
        self.settings.setValue("workspace/developerTab", self.developer_workspace.tabs.currentIndex())
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.sync()
        self.developer_workspace.shutdown()
        self.controller.stop()
        super().closeEvent(event)

    def _set_projects(self, projects: object) -> None:
        self._sync_workspace_state()

    def _sync_workspace_state(self, _state: object = None) -> None:
        project = self.controller.active_project
        selected_id = self.controller.active_project_id
        if selected_id:
            self._expanded_project_ids.add(selected_id)
        if self._composer_project_id != selected_id:
            self.input.clear()
            self._composer_project_id = selected_id
        self._render_workspace_tree()

        if project is not None:
            self.project_name.setText(project.name)
            self.project_name.setToolTip(str(project.path))
            root = self.controller.active_workspace_root
            context = self.controller.active_workspace_context
            context_key = context.key if context is not None else f"main:{project.id}"
            self.developer_workspace.set_project(project.id, root or project.path, context_key)
        else:
            self.project_name.setText("No Project")
            self.project_name.setToolTip("")
            self.developer_workspace.set_project(None, None)
        self._update_agent_task_actions()
        self._update_project_actions()

    def _render_workspace_tree(self) -> None:
        blocker = QSignalBlocker(self.project_tree)
        try:
            self.project_tree.clear()
            projects = self.controller.projects
            if not projects:
                empty = QTreeWidgetItem(["No projects yet — click + to add one"])
                empty.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.project_tree.addTopLevelItem(empty)
                return

            target_item = None
            current_key = self._current_thread_key
            draft_project_id = self.controller.state.draft_thread_project_id
            for project in projects:
                project_item = QTreeWidgetItem()
                project_item.setData(0, Qt.ItemDataRole.UserRole, project.id)
                project_item.setData(0, TREE_ITEM_KIND_ROLE, "project")
                project_item.setToolTip(0, str(project.path))
                self.project_tree.addTopLevelItem(project_item)

                row = ProjectTreeRow(project)
                font = row.name.font()
                font.setBold(project.id == self.controller.active_project_id)
                row.name.setFont(font)
                row.project_clicked.connect(self._project_row_clicked)
                row.new_thread_requested.connect(self._new_thread_for_project)
                self.project_tree.setItemWidget(project_item, 0, row)

                for summary in self.controller.threads_for_project(project.id):
                    child = QTreeWidgetItem(project_item)
                    child.setData(0, Qt.ItemDataRole.UserRole, summary)
                    child.setData(0, TREE_ITEM_KIND_ROLE, "thread")
                    child.setText(0, self._thread_label(summary))
                    child.setToolTip(0, summary.id)
                    if current_key == (summary.provider, summary.id):
                        target_item = child

                project_tasks = self.controller.agent_tasks_for_project(project.id)
                if project_tasks:
                    tasks_group = QTreeWidgetItem(project_item)
                    tasks_group.setData(0, TREE_ITEM_KIND_ROLE, "agent_tasks_group")
                    tasks_group.setText(0, "Agent Tasks")
                    tasks_group.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    for task in project_tasks:
                        child = QTreeWidgetItem(tasks_group)
                        child.setData(0, Qt.ItemDataRole.UserRole, task)
                        child.setData(0, TREE_ITEM_KIND_ROLE, "agent_task")
                        child.setText(0, self._agent_task_label(task))
                        child.setToolTip(0, f"{task.worktree_path}\n{task.branch_name}")
                        if task.id == self._current_task_id:
                            target_item = child

                project_runs = self.controller.orchestration_runs_for_project(project.id)
                if project_runs:
                    runs_group = QTreeWidgetItem(project_item)
                    runs_group.setData(0, TREE_ITEM_KIND_ROLE, "orchestration_runs_group")
                    runs_group.setText(0, "Orchestration Runs")
                    runs_group.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    for run in project_runs:
                        child = QTreeWidgetItem(runs_group)
                        child.setData(0, Qt.ItemDataRole.UserRole, run)
                        child.setData(0, TREE_ITEM_KIND_ROLE, "orchestration_run")
                        child.setText(0, f"{run.status.value} · {run.objective[:60]}")
                        child.setToolTip(0, f"Run {run.run_id}\nbase {run.base_commit}")
                        if run.run_id == self._current_run_id:
                            target_item = child

                if draft_project_id == project.id:
                    draft = QTreeWidgetItem(project_item)
                    draft.setData(0, TREE_ITEM_KIND_ROLE, "draft")
                    draft.setText(0, "New Thread (unsaved)")
                    draft.setToolTip(0, "Send a message to create this Thread")
                    if target_item is None and project.id == self.controller.active_project_id:
                        target_item = draft

                project_item.setExpanded(project.id in self._expanded_project_ids)
                if target_item is None and project.id == self.controller.active_project_id:
                    target_item = project_item

            if target_item is not None:
                self.project_tree.setCurrentItem(target_item)
        finally:
            del blocker

    @staticmethod
    def _thread_label(summary: ThreadSummary) -> str:
        title = summary.title or summary.id[:12]
        model = summary.model or "unknown model"
        return f"{title}\n{summary.provider} · {model} · {_activity(summary.updated_at)}"

    @staticmethod
    def _agent_task_label(task: AgentTask) -> str:
        model = task.model_id or "default model"
        return f"{task.status_icon} {task.title}\n{task.provider_id} · {model} · {task.status_label}"

    def _project_expanded(self, item: QTreeWidgetItem) -> None:
        project_id = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(project_id, str):
            self._expanded_project_ids.add(project_id)

    def _project_collapsed(self, item: QTreeWidgetItem) -> None:
        project_id = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(project_id, str):
            self._expanded_project_ids.discard(project_id)

    def _set_current_project(self, _project: object) -> None:
        self._sync_workspace_state()

    def _set_threads(self, summaries: object) -> None:
        self._sync_workspace_state()

    def _set_thread(self, thread: object) -> None:
        if self.controller.active_agent_task is not None:
            return
        self._current_task_id = None
        self._current_run_id = None
        self.timeline.clear()
        if isinstance(thread, Thread):
            self._current_thread_key = (thread.provider, thread.id)
            self.thread_name.setText(thread.title or thread.id[:12])
            self.timeline.load_thread(thread)
        else:
            self._current_thread_key = None
            self.thread_name.setText("New Thread")
        self._set_threads(self.controller.workspace_threads)

    def _set_agent_tasks(self, _tasks: object) -> None:
        self._render_workspace_tree()
        self._update_agent_task_actions()

    def _set_orchestration_runs(self, _runs: object) -> None:
        self._render_workspace_tree()

    def _agent_task_updated(self, task: object) -> None:
        if not isinstance(task, AgentTask):
            return
        if task.id == self._current_task_id:
            self.thread_name.setText(f"{task.title} · {task.status_label}")
            self._set_agent_status(task.status_label)
        self._set_agent_tasks(self.controller.agent_tasks)

    def _set_agent_task(self, task: object, thread: object) -> None:
        if not isinstance(task, AgentTask):
            return
        self._current_task_id = task.id
        self._current_run_id = None
        self._current_thread_key = None
        self.timeline.clear()
        self.thread_name.setText(f"{task.title} · {task.status_label}")
        self._set_agent_status(task.status_label)
        if isinstance(thread, Thread):
            self.timeline.load_thread(thread)
        for request in self.controller._pending_task_approvals(task.id):
            self.timeline.add_approval_request(request)
        self._sync_workspace_state()
        self._set_provider(task.provider_id)
        model_index = self.models.findData(task.model_id)
        if model_index >= 0:
            self.models.setCurrentIndex(model_index)
        self._update_agent_task_actions()

    def _agent_task_timeline_event(self, task_id: str, kind: str, data: object) -> None:
        if task_id != self._current_task_id:
            return
        self.timeline.apply_event(kind, data)
        self.developer_workspace.handle_timeline_event(kind, data)

    def _workspace_context_changed(self, _context: object) -> None:
        self._sync_workspace_state()

    def _update_agent_task_actions(self) -> None:
        task = self.controller.active_agent_task
        has_task = task is not None
        self.cancel_agent_task_button.setVisible(has_task and task.status in {AgentTaskStatus.CREATING, AgentTaskStatus.READY, AgentTaskStatus.RUNNING, AgentTaskStatus.WAITING_APPROVAL})
        self.cleanup_agent_task_button.setVisible(has_task and task.status in {AgentTaskStatus.COMPLETED, AgentTaskStatus.CANCELLED, AgentTaskStatus.FAILED, AgentTaskStatus.ORPHANED})
        self.new_agent_task_button.setEnabled(self.controller.current_project is not None)

    def _set_models(self, models: object, default: object) -> None:
        self.models.blockSignals(True)
        self.models.clear()
        selected_index = 0
        for model in models if isinstance(models, list) else []:
            if not isinstance(model, dict) or not isinstance(model.get("model"), str):
                continue
            model_id = model["model"]
            self.models.addItem(str(model.get("displayName", model_id)), model_id)
            if model_id == default:
                selected_index = self.models.count() - 1
        self.models.setCurrentIndex(selected_index)
        self.models.setEnabled(self.models.count() > 0)
        self.models.blockSignals(False)

    def _set_provider(self, provider_id: str) -> None:
        index = self.provider.findData(provider_id)
        if index >= 0 and index != self.provider.currentIndex():
            self.provider.blockSignals(True)
            self.provider.setCurrentIndex(index)
            self.provider.blockSignals(False)

    def _tree_item_selected(
        self,
        current: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        if current is None:
            return
        kind = current.data(0, TREE_ITEM_KIND_ROLE)
        if kind == "project":
            project_id = current.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(project_id, str):
                self.controller.activate_project(project_id, source="project-tree")
        elif kind == "agent_task":
            task = current.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(task, AgentTask):
                self.controller.activate_agent_task(task.id, source="agent-task-tree")
        elif kind == "agent_tasks_group":
            parent = current.parent()
            project_id = parent.data(0, Qt.ItemDataRole.UserRole) if parent else None
            if isinstance(project_id, str):
                self.controller.activate_project(project_id, source="agent-task-group")
        elif kind == "orchestration_run":
            run = current.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(run, OrchestrationRun):
                self._open_orchestration(run.run_id)
        elif kind == "orchestration_runs_group":
            parent = current.parent()
            project_id = parent.data(0, Qt.ItemDataRole.UserRole) if parent else None
            if isinstance(project_id, str):
                self.controller.activate_project(project_id, source="orchestration-group")
        elif kind == "thread":
            summary = current.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(summary, ThreadSummary):
                self.controller.resume_thread(summary)
        elif kind == "draft":
            parent = current.parent()
            project_id = parent.data(0, Qt.ItemDataRole.UserRole) if parent else None
            if isinstance(project_id, str):
                self._new_thread_for_project(project_id)

    def _project_row_clicked(self, project_id: str) -> None:
        for index in range(self.project_tree.topLevelItemCount()):
            item = self.project_tree.topLevelItem(index)
            if item.data(0, Qt.ItemDataRole.UserRole) == project_id:
                self.project_tree.setCurrentItem(item)
                return

    def _new_thread_for_project(self, project_id: str) -> None:
        if self.controller.new_thread_for_project(project_id):
            self._prepare_new_thread_ui()

    def _new_thread(self) -> None:
        if self.controller.new_thread():
            self._prepare_new_thread_ui()

    def _new_agent_task(self) -> None:
        if self.controller.current_project is None:
            self._error("Add or open a Project before creating an Agent Task")
            return
        dialog = AgentTaskDialog(self.controller, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        title, prompt, provider, model, effort = dialog.values()
        task = self.controller.create_agent_task(
            title,
            prompt,
            provider_id=provider,
            model_id=model,
            reasoning_effort=effort,
        )
        if task is not None:
            self.statusBar().showMessage(f"Created Agent Task: {task.title}", 5000)

    def _new_orchestration(self) -> None:
        if self.controller.current_project is None:
            self._error("Add or open a Project before creating an Orchestration Run")
            return
        dialog = OrchestrationDialog(self.controller, self)
        self._orchestration_dialog = dialog
        dialog.exec()
        self._orchestration_dialog = None

    def _open_orchestration(self, run_id: str) -> None:
        dialog = OrchestrationDialog(self.controller, self, run_id=run_id)
        self._orchestration_dialog = dialog
        dialog.exec()
        self._orchestration_dialog = None

    def _cancel_agent_task(self) -> None:
        task = self.controller.active_agent_task
        if task is not None:
            self.controller.cancel_agent_task(task.id)

    def _cleanup_agent_task(self) -> None:
        task = self.controller.active_agent_task
        if task is None:
            return
        answer = QMessageBox.question(
            self,
            "Cleanup Agent Task",
            "Remove this clean Agent Task worktree? Uncommitted changes are never force-deleted.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.cleanup_agent_task(task.id, terminal_active=self.developer_workspace.any_terminal_active())

    def _prepare_new_thread_ui(self) -> None:
        self.input.clear()
        self.statusBar().showMessage("New Thread ready — send a message to create it", 5000)

    def _provider_changed(self, index: int) -> None:
        provider_id = self.provider.itemData(index)
        if isinstance(provider_id, str):
            self.controller.set_provider(provider_id)

    def _model_changed(self, index: int) -> None:
        self.controller.set_model(self.models.itemData(index) if index >= 0 else None)

    def _reasoning_changed(self, index: int) -> None:
        self.controller.set_reasoning_effort(self.reasoning.itemData(index) if index >= 0 else None)

    def _add_project(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Open Project Directory")
        if path:
            try:
                self.controller.add_project(Path(path))
            except ValueError as exc:
                self._error(str(exc))

    def _remove_project(self) -> None:
        project = self.controller.current_project
        if project is None:
            return
        answer = QMessageBox.question(self, "Remove Project", f"Remove {project.name} from the project list?")
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.remove_project(project.id)

    def _pin_project(self) -> None:
        project = self.controller.current_project
        if project is not None:
            self.controller.set_project_pinned(project.id, not project.pinned)

    def _rename_thread(self) -> None:
        summary = self._selected_summary()
        if summary is None:
            return
        name, accepted = QInputDialog.getText(self, "Rename Thread", "Thread name:", text=summary.title or "")
        if accepted:
            self.controller.rename_thread(summary, name)

    def _archive_thread(self) -> None:
        summary = self._selected_summary()
        if summary is not None:
            self.controller.archive_thread(summary)

    def _delete_thread(self) -> None:
        summary = self._selected_summary()
        if summary is None:
            return
        answer = QMessageBox.question(self, "Delete Thread", "Delete this Thread from Codex?")
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.delete_thread(summary)

    def _show_thread_context_menu(self, position) -> None:
        item = self.project_tree.itemAt(position)
        if item is None or item.data(0, TREE_ITEM_KIND_ROLE) != "thread":
            return
        summary = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(summary, ThreadSummary):
            return
        blocker = QSignalBlocker(self.project_tree)
        self.project_tree.setCurrentItem(item)
        del blocker

        menu = QMenu(self)
        rename = menu.addAction("Rename")
        archive = menu.addAction("Archive")
        delete = menu.addAction("Delete")
        chosen = menu.exec(self.project_tree.viewport().mapToGlobal(position))
        if chosen is rename:
            self._rename_thread()
        elif chosen is archive:
            self._archive_thread()
        elif chosen is delete:
            self._delete_thread()

    def _selected_summary(self) -> ThreadSummary | None:
        item = self.project_tree.currentItem()
        value = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        return value if isinstance(value, ThreadSummary) else None

    def _show_settings(self) -> None:
        dialog = SettingsDialog(self.controller, self)
        dialog.exec()

    def _send(self) -> None:
        text = self.input.toPlainText().strip()
        if text:
            self.input.clear()
            self.controller.send_text(text)

    def _set_busy(self, busy: bool) -> None:
        task = self.controller.active_agent_task
        task_busy = task is not None and task.status in {AgentTaskStatus.CREATING, AgentTaskStatus.RUNNING, AgentTaskStatus.WAITING_APPROVAL}
        self.send.setEnabled(not busy and not task_busy and self.controller.current_project is not None)
        self.statusBar().showMessage("Working…" if busy else "Ready")

    def _set_agent_status(self, status: str) -> None:
        self.agent_status.setText(f"Agent Status: {status}")

    def _toggle_developer_workspace(self, visible: bool) -> None:
        self.developer_workspace.setVisible(visible)
        if visible:
            self.developer_workspace.refresh_git()

    def _set_history_loading(self, loading: bool) -> None:
        if loading and not self.controller.busy:
            self.statusBar().showMessage("Loading Threads…")

    def _error(self, message: str) -> None:
        self.statusBar().showMessage(message, 10000)

    def _update_project_actions(self) -> None:
        project = self.controller.current_project
        enabled = project is not None
        self.remove_project_button.setEnabled(enabled)
        self.pin_project_button.setEnabled(enabled)
        self.new_thread_toolbar.setEnabled(enabled)
        task = self.controller.active_agent_task
        task_busy = task is not None and task.status in {AgentTaskStatus.CREATING, AgentTaskStatus.RUNNING, AgentTaskStatus.WAITING_APPROVAL}
        self.send.setEnabled(enabled and not self.controller.busy and not task_busy)
        self.new_agent_task_button.setEnabled(enabled)
        if project is not None:
            self.pin_project_button.setText("★" if project.pinned else "☆")

def _thread_group(value: str | None) -> str:
    if not value:
        return "Earlier"
    try:
        if value.isdigit():
            current = datetime.fromtimestamp(int(value), tz=None).date()
        else:
            current = datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (TypeError, ValueError, OverflowError):
        return "Earlier"
    delta = (date.today() - current).days
    if delta <= 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    return "Earlier"


def _activity(value: str | None) -> str:
    if not value:
        return "unknown"
    if value.isdigit():
        return datetime.fromtimestamp(int(value)).strftime("%H:%M")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%m-%d %H:%M")
    except ValueError:
        return value[:16]
