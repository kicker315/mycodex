from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from mint_codex.workspace.file_access import FileAccessError, read_project_file
from mint_codex.workspace.git_workspace import GitChange, GitDiffResult, GitStatusSnapshot, GitWorkspace
from mint_codex.workspace.terminal import TerminalOutputFilter, TerminalSession


class DeveloperWorkspacePanel(QWidget):
    """Project-scoped review and terminal workspace for the active Project."""

    file_selected = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("developer_workspace")
        self._project_id: str | None = None
        self._root: Path | None = None
        self._context_key: str | None = None
        self._changes: dict[str, GitChange] = {}
        self._terminals: dict[str, TerminalSession] = {}
        self._terminal_filters: dict[str, TerminalOutputFilter] = {}
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(250)
        self._refresh_timer.timeout.connect(self.refresh_git)
        self._build_ui()
        self.git = GitWorkspace(self)
        self.git.status_changed.connect(self._status_changed)
        self.git.diff_changed.connect(self._diff_changed)
        self.git.error.connect(self._git_error)

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)
        header = QHBoxLayout()
        title = QLabel("Developer Workspace")
        title.setProperty("developerRole", "title")
        self.context = QLabel("No Project")
        self.context.setProperty("developerRole", "context")
        self.refresh_button = QToolButton()
        self.refresh_button.setText("↻")
        self.refresh_button.setToolTip("Refresh Git status")
        header.addWidget(title)
        header.addWidget(self.context, 1)
        header.addWidget(self.refresh_button)
        root_layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("developer_workspace_tabs")
        self.tabs.addTab(self._build_changes_tab(), "Changes")
        self.tabs.addTab(self._build_preview_tab(), "File Preview")
        self.tabs.addTab(self._build_terminal_tab(), "Terminal")
        root_layout.addWidget(self.tabs, 1)

        self.refresh_button.clicked.connect(self.refresh_git)

    def _build_changes_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.git_status = QLabel("No Project")
        self.git_status.setObjectName("git_status")
        layout.addWidget(self.git_status)
        self.changed_files = QListWidget()
        self.changed_files.setObjectName("changed_files")
        self.changed_files.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.changed_files.itemSelectionChanged.connect(self._changed_file_selected)
        layout.addWidget(self.changed_files, 1)
        self.diff_path = QLabel("Select a changed file")
        self.diff_path.setObjectName("diff_path")
        layout.addWidget(self.diff_path)
        self.diff_view = QPlainTextEdit()
        self.diff_view.setObjectName("diff_view")
        self.diff_view.setReadOnly(True)
        self.diff_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.diff_view.setFont(QFont("monospace"))
        layout.addWidget(self.diff_view, 2)
        return tab

    def _build_preview_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        input_row = QHBoxLayout()
        self.preview_input = QLineEdit()
        self.preview_input.setObjectName("preview_input")
        self.preview_input.setPlaceholderText("Relative path inside the Project")
        self.preview_button = QPushButton("Preview")
        input_row.addWidget(self.preview_input, 1)
        input_row.addWidget(self.preview_button)
        layout.addLayout(input_row)
        self.preview_path = QLabel("No file selected")
        self.preview_path.setObjectName("preview_path")
        layout.addWidget(self.preview_path)
        self.file_preview = QPlainTextEdit()
        self.file_preview.setObjectName("file_preview")
        self.file_preview.setReadOnly(True)
        self.file_preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.file_preview.setFont(QFont("monospace"))
        layout.addWidget(self.file_preview, 1)
        self.preview_button.clicked.connect(self._preview_from_input)
        return tab

    def _build_terminal_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.terminal_context = QLabel("No Project")
        self.terminal_context.setObjectName("terminal_context")
        layout.addWidget(self.terminal_context)
        terminal_buttons = QHBoxLayout()
        self.terminal_start = QPushButton("Start")
        self.terminal_interrupt = QPushButton("Ctrl+C")
        self.terminal_interrupt.setEnabled(False)
        terminal_buttons.addWidget(self.terminal_start)
        terminal_buttons.addWidget(self.terminal_interrupt)
        terminal_buttons.addStretch(1)
        layout.addLayout(terminal_buttons)
        self.terminal_output = QPlainTextEdit()
        self.terminal_output.setObjectName("terminal_output")
        self.terminal_output.setReadOnly(True)
        self.terminal_output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.terminal_output.setFont(QFont("monospace"))
        layout.addWidget(self.terminal_output, 1)
        input_row = QHBoxLayout()
        self.terminal_input = QPlainTextEdit()
        self.terminal_input.setObjectName("terminal_input")
        self.terminal_input.setPlaceholderText("Terminal input")
        self.terminal_input.setMaximumHeight(60)
        self.terminal_send = QPushButton("Send")
        input_row.addWidget(self.terminal_input, 1)
        input_row.addWidget(self.terminal_send)
        layout.addLayout(input_row)
        self.terminal_start.clicked.connect(self.start_terminal)
        self.terminal_interrupt.clicked.connect(self.interrupt_terminal)
        self.terminal_send.clicked.connect(self.send_terminal_input)
        return tab

    @property
    def project_id(self) -> str | None:
        return self._project_id

    @property
    def project_root(self) -> Path | None:
        return self._root

    @property
    def terminal(self) -> TerminalSession | None:
        return self._terminals.get(self._context_key or "")

    @property
    def terminal_active(self) -> bool:
        terminal = self.terminal
        return terminal is not None and terminal.is_running

    def any_terminal_active(self) -> bool:
        return any(terminal.is_running for terminal in self._terminals.values())

    def set_project(self, project_id: str | None, root: Path | None, context_key: str | None = None) -> None:
        resolved = root.expanduser().resolve(strict=False) if root is not None else None
        key = context_key or (f"main:{project_id}" if project_id else "none")
        if project_id == self._project_id and resolved == self._root and key == self._context_key:
            return
        self._project_id = project_id
        self._root = resolved
        self._context_key = key
        self._changes.clear()
        self.changed_files.clear()
        self.diff_path.setText("Select a changed file")
        self.diff_view.clear()
        self.preview_path.setText("No file selected")
        self.preview_input.clear()
        self.file_preview.clear()
        context = str(resolved) if resolved else "No Project"
        self.context.setText(context)
        self.terminal_context.setText(context)
        self.git.set_root(resolved)
        self.refresh_git()

    def refresh_git(self) -> None:
        self.git.refresh()

    def handle_timeline_event(self, kind: str, data: object) -> None:
        if kind == "turn_completed" or (kind == "item_completed" and isinstance(data, dict) and data.get("type") == "fileChange"):
            self._refresh_timer.start()

    def open_changed_file(self, path: str) -> None:
        if self._root is None:
            return
        item = self._find_change(path)
        if item is not None:
            self.changed_files.setCurrentRow(self._row_for_path(item.path))
        self._show_file(path, item)

    def _status_changed(self, snapshot: object) -> None:
        if not isinstance(snapshot, GitStatusSnapshot) or snapshot.root != self._root:
            return
        self._changes = {change.path: change for change in snapshot.changes}
        self.changed_files.clear()
        for change in snapshot.changes:
            item = QListWidgetItem(change.label)
            item.setData(Qt.ItemDataRole.UserRole, change.path)
            item.setToolTip(change.path)
            self.changed_files.addItem(item)
        self.git_status.setText(snapshot.message or f"{len(snapshot.changes)} changed file(s)")
        if snapshot.message:
            self.diff_view.setPlainText("")

    def _diff_changed(self, result: object) -> None:
        if not isinstance(result, GitDiffResult) or result.root != self._root:
            return
        self.diff_path.setText(result.path)
        if result.error:
            self.diff_view.setPlainText(result.error)
        else:
            self.diff_view.setPlainText(result.text or "No diff")

    def _git_error(self, message: str) -> None:
        self.git_status.setText(message)

    def _changed_file_selected(self) -> None:
        item = self.changed_files.currentItem()
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(path, str):
            self._show_file(path, self._changes.get(path))

    def _show_file(self, path: str, change: GitChange | None) -> None:
        if self._root is None:
            return
        self.diff_path.setText(path)
        self.git.diff_for(path, change)
        self.preview_file(path)
        self.file_selected.emit(path)

    def preview_file(self, path: str) -> None:
        if self._root is None:
            return
        self.preview_input.setText(path)
        self.preview_path.setText(path)
        try:
            preview = read_project_file(self._root, path)
        except FileAccessError as exc:
            self.file_preview.setPlainText(str(exc))
            return
        numbered = "\n".join(f"{line_no:>5} │ {line}" for line_no, line in enumerate(preview.text.splitlines(), 1))
        self.file_preview.setPlainText(numbered)

    def _preview_from_input(self) -> None:
        path = self.preview_input.text().strip()
        if path:
            self.preview_file(path)

    def start_terminal(self) -> None:
        if self._root is None:
            return
        key = self._context_key or "none"
        terminal = self._terminals.get(key)
        if terminal is None:
            terminal = TerminalSession(self._root, self)
            self._terminals[key] = terminal
            self._terminal_filters[key] = TerminalOutputFilter()
            terminal.output.connect(lambda text, terminal_key=key: self._terminal_output(terminal_key, text))
            terminal.state_changed.connect(lambda state, terminal_key=key: self._terminal_state(terminal_key, state))
            terminal.error.connect(lambda message, terminal_key=key: self._terminal_error_for(terminal_key, message))
            terminal.finished.connect(lambda _code, terminal_key=key: self._terminal_state(terminal_key, "Exited"))
        if terminal.start():
            self.terminal_output.appendPlainText(f"\n[terminal cwd: {self._root}]\n")

    def send_terminal_input(self) -> None:
        terminal = self.terminal
        if terminal is None or not terminal.is_running:
            return
        text = self.terminal_input.toPlainText()
        if text:
            terminal.send_input(text + ("" if text.endswith("\n") else "\n"))
            self.terminal_input.clear()

    def interrupt_terminal(self) -> None:
        terminal = self.terminal
        if terminal is not None:
            terminal.interrupt()

    def _terminal_state_changed(self, state: str) -> None:
        running = state in {"Running", "Starting"}
        self.terminal_start.setEnabled(not running)
        self.terminal_interrupt.setEnabled(running)
        if state == "Exited":
            self.terminal_output.appendPlainText("\n[terminal exited]\n")

    def _terminal_error(self, message: str) -> None:
        self.terminal_output.appendPlainText(f"\n[terminal error: {message}]\n")

    def _terminal_output(self, key: str, text: str) -> None:
        if key == self._context_key:
            output_filter = self._terminal_filters.setdefault(key, TerminalOutputFilter())
            rendered = output_filter.feed(text)
            if rendered:
                self.terminal_output.insertPlainText(rendered)

    def _terminal_state(self, key: str, state: str) -> None:
        if key == self._context_key:
            self._terminal_state_changed(state)

    def _terminal_error_for(self, key: str, message: str) -> None:
        if key == self._context_key:
            self._terminal_error(message)

    def _stop_terminal(self) -> None:
        terminal = self.terminal
        if terminal is not None:
            terminal.stop()
            self._terminals.pop(self._context_key or "", None)
            terminal.deleteLater()
            QCoreApplication.sendPostedEvents(terminal, QEvent.Type.DeferredDelete)
            self._terminal_filters.pop(self._context_key or "", None)
        self.terminal_output.clear()
        self.terminal_start.setEnabled(True)
        self.terminal_interrupt.setEnabled(False)

    def shutdown(self) -> None:
        self._refresh_timer.stop()
        for terminal in list(self._terminals.values()):
            terminal.stop()
            terminal.deleteLater()
            QCoreApplication.sendPostedEvents(terminal, QEvent.Type.DeferredDelete)
        self._terminals.clear()
        self._terminal_filters.clear()
        self.git.close()

    def _find_change(self, path: str) -> GitChange | None:
        return self._changes.get(path)

    def _row_for_path(self, path: str) -> int:
        for index in range(self.changed_files.count()):
            if self.changed_files.item(index).data(Qt.ItemDataRole.UserRole) == path:
                return index
        return -1
