from PySide6.QtCore import QSettings
import subprocess

from mint_codex.ui.developer_workspace import DeveloperWorkspacePanel
from mint_codex.ui.main_window import MainWindow, SettingsDialog
from mint_codex.workspace.controller import WorkspaceController
from mint_codex.workspace.registry import ProjectRegistry


def make_workspace(tmp_path):
    return WorkspaceController(
        cwd=tmp_path,
        registry=ProjectRegistry(tmp_path / "workspace.sqlite3"),
    )


def test_main_window_uses_project_and_thread_sidebars_without_api_key_editor(qapp, tmp_path):
    workspace = make_workspace(tmp_path)
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    window = MainWindow(workspace, settings=settings)

    assert window.project_tree.objectName() == "project_tree"
    assert window.timeline.objectName() == "agent_timeline"
    assert window.agent_status.text() == "Agent Status: Ready"
    assert not hasattr(window, "rename_thread_button")
    assert not hasattr(window, "archive_thread_button")
    assert not hasattr(window, "delete_thread_button")
    assert not hasattr(window, "deepseek_key_input")


def test_settings_dialog_owns_deepseek_key_editor(qapp, tmp_path):
    workspace = make_workspace(tmp_path)
    applied = []
    workspace.set_provider_api_key = lambda provider, key: (applied.append((provider, key)), True)[1]
    dialog = SettingsDialog(workspace)

    assert dialog.deepseek_key_input.echoMode() == dialog.deepseek_key_input.EchoMode.Password
    dialog.deepseek_key_input.setText("fixture-value")
    dialog.save_key.click()

    assert applied == [("deepseek", "fixture-value")]
    assert dialog.deepseek_key_input.text() == ""


def test_main_window_has_collapsible_developer_workspace_and_three_contexts(qapp, tmp_path):
    workspace = make_workspace(tmp_path)
    project_a_path = tmp_path / "project-a"
    project_a_path.mkdir()
    project_a = workspace.add_project(project_a_path, name="project-a")
    window = MainWindow(workspace, settings=QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))

    assert window.developer_workspace.objectName() == "developer_workspace"
    assert window.developer_workspace.tabs.count() == 3
    assert len(window.centralWidget().sizes()) == 3
    window.developer_workspace_toggle.click()
    assert window.developer_workspace.isHidden() is True
    window.developer_workspace_toggle.click()
    assert window.developer_workspace.isHidden() is False


def test_developer_panel_switches_git_file_and_terminal_context(qapp, tmp_path):
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    (project_a / "a.py").write_text("print('a')\n", encoding="utf-8")
    (project_b / "b.py").write_text("print('b')\n", encoding="utf-8")
    panel = DeveloperWorkspacePanel()

    panel.set_project("project-a", project_a)
    assert panel.project_id == "project-a"
    assert panel.project_root == project_a.resolve()
    panel.preview_file("a.py")
    assert "print('a')" in panel.file_preview.toPlainText()
    panel.set_project("project-b", project_b)
    assert panel.project_id == "project-b"
    assert panel.project_root == project_b.resolve()
    assert panel.terminal is None
    assert panel.file_preview.toPlainText() == ""
    panel.preview_file("../project-a/a.py")
    assert "outside" in panel.file_preview.toPlainText().lower()
    panel.shutdown()


def test_developer_panel_refreshes_git_after_turn_without_event_loop(qapp, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.name", "Test"], check=True)
    source = project / "source.py"
    source.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "source.py"], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "initial"], check=True)
    panel = DeveloperWorkspacePanel()
    snapshots = []
    panel.git.status_changed.connect(snapshots.append)
    panel.set_project("project", project)

    from PySide6.QtCore import QEventLoop, QTimer

    def wait_for(count):
        loop = QEventLoop()
        QTimer.singleShot(2500, loop.quit)
        while len(snapshots) < count:
            QTimer.singleShot(30, loop.quit)
            loop.exec()
            if len(snapshots) >= count:
                break

    wait_for(1)
    source.write_text("after\n", encoding="utf-8")
    panel.handle_timeline_event("turn_completed", {})
    wait_for(2)
    assert len(snapshots) >= 2
    assert any(change.path == "source.py" for change in snapshots[-1].changes)
    panel.shutdown()


def test_developer_workspace_layout_state_restores(qapp, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    workspace = make_workspace(tmp_path)
    workspace.add_project(project, name="project")
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    first = MainWindow(workspace, settings=settings)
    first.centralWidget().setSizes([230, 570, 340])
    first.developer_workspace.tabs.setCurrentIndex(1)
    first.close()

    saved_width = int(settings.value("window/developerPanelWidth"))
    second = MainWindow(workspace, settings=settings)
    assert second.developer_workspace.tabs.currentIndex() == 1
    assert saved_width > 0
    assert second.centralWidget().sizes()[2] == saved_width
    second.close()
