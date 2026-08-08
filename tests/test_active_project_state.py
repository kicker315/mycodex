from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QToolButton

from mint_codex.models.session import Thread, ThreadSummary
from mint_codex.ui.main_window import MainWindow
from mint_codex.workspace.controller import WorkspaceController
from mint_codex.workspace.registry import ProjectRegistry


def _workspace(tmp_path: Path) -> tuple[WorkspaceController, object, object]:
    opendata = tmp_path / "opendata"
    openocta = tmp_path / "openocta"
    opendata.mkdir()
    openocta.mkdir()
    workspace = WorkspaceController(
        cwd=tmp_path,
        registry=ProjectRegistry(tmp_path / "workspace.sqlite3"),
    )
    project_a = workspace.add_project(opendata, name="opendata")
    project_b = workspace.add_project(openocta, name="openocta")
    return workspace, project_a, project_b


def _sidebar_project_id(window: MainWindow) -> str | None:
    item = window.project_tree.currentItem()
    value = item.data(0, Qt.ItemDataRole.UserRole) if item else None
    if isinstance(value, str):
        return value
    parent = item.parent() if item else None
    parent_value = parent.data(0, Qt.ItemDataRole.UserRole) if parent else None
    return parent_value if isinstance(parent_value, str) else None


def _sidebar_item(window: MainWindow, project_id: str):
    for index in range(window.project_tree.topLevelItemCount()):
        item = window.project_tree.topLevelItem(index)
        if item.data(0, Qt.ItemDataRole.UserRole) == project_id:
            return item
    raise AssertionError(f"Project {project_id} is not in the Sidebar")


def test_click_and_programmatic_activation_keep_all_project_views_in_sync(qapp, tmp_path):
    workspace, project_a, project_b = _workspace(tmp_path)
    window = MainWindow(
        workspace,
        settings=QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat),
    )

    window.project_tree.setCurrentItem(_sidebar_item(window, project_a.id))
    assert workspace.active_project_id == project_a.id
    assert _sidebar_project_id(window) == project_a.id
    assert window.project_name.text() == "opendata"
    assert workspace.cwd == project_a.path

    window.input.setPlainText("draft belongs to A")
    workspace.activate_project(project_b.id, source="test-programmatic")
    assert workspace.active_project_id == project_b.id
    assert _sidebar_project_id(window) == project_b.id
    assert window.project_name.text() == "openocta"
    assert workspace.cwd == project_b.path
    assert window.input.toPlainText() == ""


def test_restore_project_b_selects_project_b_in_sidebar(qapp, tmp_path):
    workspace, _project_a, project_b = _workspace(tmp_path)
    workspace.activate_project(project_b.id, source="test-restore")
    workspace.registry.close()

    restored = WorkspaceController(
        cwd=tmp_path,
        registry=ProjectRegistry(tmp_path / "workspace.sqlite3"),
    )
    window = MainWindow(
        restored,
        settings=QSettings(str(tmp_path / "restored.ini"), QSettings.Format.IniFormat),
    )

    assert restored.active_project_id == project_b.id
    assert _sidebar_project_id(window) == project_b.id
    assert window.project_name.text() == "openocta"


def test_resume_thread_activates_its_project_before_routing(qapp, tmp_path):
    workspace, project_a, project_b = _workspace(tmp_path)
    window = MainWindow(
        workspace,
        settings=QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat),
    )
    workspace.activate_project(project_a.id, source="test-current")
    summary = ThreadSummary(
        "thread-b",
        "openai",
        "gpt-5",
        "Thread B",
        str(project_b.path),
        "2026-08-08T10:00:00Z",
        project_b.id,
    )
    workspace.registry.upsert_thread(project_b.id, summary)
    backend = workspace.backends["openai"]
    requests = []
    backend._ready = True

    def request(method, params=None, callback=None):
        requests.append((method, params))
        if method == "thread/list":
            callback({"data": []}, None)
            return 1
        callback(
            {
                "thread": {
                    "id": "thread-b",
                    "modelProvider": "openai",
                    "cwd": str(project_b.path),
                },
                "model": "gpt-5",
                "cwd": str(project_b.path),
            },
            None,
        )
        return 1

    backend.request = request
    workspace.resume_thread(summary)

    assert workspace.active_project_id == project_b.id
    assert _sidebar_project_id(window) == project_b.id
    assert window.project_name.text() == "openocta"
    assert workspace.cwd == project_b.path
    resume_requests = [request for request in requests if request[0] == "thread/resume"]
    assert resume_requests
    assert resume_requests[0][1]["cwd"] == str(project_b.path)
    assert workspace.current_thread is not None
    assert workspace.current_thread.project_id == project_b.id


def test_refresh_and_pin_reorder_preserve_active_project_id(qapp, tmp_path):
    workspace, project_a, project_b = _workspace(tmp_path)
    window = MainWindow(
        workspace,
        settings=QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat),
    )
    workspace.activate_project(project_b.id, source="test-active")

    workspace.refresh_projects()
    workspace.set_project_pinned(project_a.id, True)

    assert workspace.active_project_id == project_b.id
    assert _sidebar_project_id(window) == project_b.id
    assert window.project_name.text() == "openocta"


def test_removing_active_project_activates_consistent_fallback(qapp, tmp_path):
    workspace, project_a, project_b = _workspace(tmp_path)
    window = MainWindow(
        workspace,
        settings=QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat),
    )
    workspace.activate_project(project_b.id, source="test-active")

    workspace.remove_project(project_b.id)

    assert workspace.active_project_id == project_a.id
    assert _sidebar_project_id(window) == project_a.id
    assert window.project_name.text() == "opendata"
    assert workspace.cwd == project_a.path


def test_project_cwd_mismatch_rejects_turn(qapp, tmp_path):
    workspace, project_a, project_b = _workspace(tmp_path)
    workspace.activate_project(project_a.id, source="test-active")
    backend = workspace.backends["openai"]
    backend._ready = True
    requests = []
    backend.request = lambda method, params=None, callback=None: requests.append((method, params))
    errors = []
    workspace.error.connect(errors.append)

    workspace.cwd = project_b.path
    workspace.send_text("must not run in B")

    assert requests == []
    assert errors
    assert "mismatch" in errors[-1].lower()


def test_stale_history_response_cannot_repopulate_new_project(qapp, tmp_path):
    workspace, project_a, project_b = _workspace(tmp_path)
    backend = workspace.backends["openai"]
    backend._ready = True
    callbacks = []
    backend.request = lambda _method, _params=None, callback=None: (callbacks.append(callback), 1)[1]

    workspace.activate_project(project_a.id, source="test-A")
    workspace.activate_project(project_b.id, source="test-B")

    callbacks[0]({"data": [{"id": "stale-A", "cwd": str(project_a.path)}]}, None)

    assert workspace.active_project_id == project_b.id
    assert workspace.registry.list_threads(project_b.id) == []


def test_stale_thread_start_response_cannot_create_thread_in_new_project(qapp, tmp_path):
    workspace, project_a, project_b = _workspace(tmp_path)
    backend = workspace.backends["openai"]
    backend._ready = True
    callbacks = []
    requests = []

    def request(method, params=None, callback=None):
        requests.append((method, params))
        callbacks.append(callback)
        return 1

    backend.request = request
    workspace.activate_project(project_a.id, source="test-A")
    workspace.send_text("start in A")
    workspace.activate_project(project_b.id, source="test-B")
    callbacks[1](
        {
            "thread": {"id": "stale-thread", "modelProvider": "openai", "cwd": str(project_a.path)},
            "model": "gpt-5",
        },
        None,
    )

    assert workspace.active_project_id == project_b.id
    assert workspace.current_thread is None
    assert [method for method, _params in requests].count("turn/start") == 0


def test_new_thread_button_resets_composer_and_enters_new_thread_state(qapp, tmp_path):
    workspace, _project_a, project_b = _workspace(tmp_path)
    window = MainWindow(
        workspace,
        settings=QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat),
    )
    workspace._replace_current(
        Thread(
            "thread-current",
            "openai",
            "gpt-5",
            "Current Thread",
            str(project_b.path),
            project_id=project_b.id,
        )
    )
    window.input.setPlainText("draft from old thread")

    window.new_thread_toolbar.click()
    qapp.processEvents()

    assert workspace.current_thread is None
    assert window.thread_name.text() == "New Thread"
    assert window.input.toPlainText() == ""
    assert "New Thread ready" in window.statusBar().currentMessage()


def test_project_tree_mounts_threads_and_project_plus_starts_project_draft(qapp, tmp_path):
    workspace, project_a, project_b = _workspace(tmp_path)
    workspace.registry.upsert_thread(
        project_a.id,
        ThreadSummary("thread-a", "openai", "gpt-5", "A thread", str(project_a.path)),
    )
    workspace.registry.upsert_thread(
        project_b.id,
        ThreadSummary("thread-b", "openai", "gpt-5", "B thread", str(project_b.path)),
    )
    window = MainWindow(
        workspace,
        settings=QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat),
    )
    window.input.setPlainText("draft from another thread")

    assert window.project_tree.topLevelItemCount() == 2
    first_project = _sidebar_item(window, project_a.id)
    assert first_project.data(0, Qt.ItemDataRole.UserRole) == project_a.id
    assert first_project.childCount() == 1
    assert first_project.child(0).data(0, Qt.ItemDataRole.UserRole).id == "thread-a"

    project_row = window.project_tree.itemWidget(first_project, 0)
    plus = project_row.findChild(QToolButton, "new_thread_for_project")
    assert plus is not None
    plus.click()
    qapp.processEvents()

    assert workspace.active_project_id == project_a.id
    assert workspace.current_thread is None
    assert window.input.toPlainText() == ""
    first_project = _sidebar_item(window, project_a.id)
    assert first_project.childCount() == 2
    assert first_project.child(1).text(0) == "New Thread (unsaved)"


def test_project_draft_becomes_real_thread_after_first_message(qapp, tmp_path):
    workspace, project_a, _project_b = _workspace(tmp_path)
    window = MainWindow(
        workspace,
        settings=QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat),
    )
    plus = window.project_tree.itemWidget(_sidebar_item(window, project_a.id), 0).findChild(
        QToolButton, "new_thread_for_project"
    )
    assert plus is not None
    plus.click()

    backend = workspace.backends["openai"]
    backend._ready = True

    def request(method, params=None, callback=None):
        if method == "thread/start":
            callback(
                {
                    "thread": {
                        "id": "created-a",
                        "modelProvider": "openai",
                        "cwd": str(project_a.path),
                    },
                    "model": "gpt-5",
                },
                None,
            )
        elif method == "turn/start":
            callback({"turn": {"id": "turn-a", "status": "inProgress"}}, None)
        return 1

    backend.request = request
    window.input.setPlainText("create the real thread")
    window.send.click()
    qapp.processEvents()

    project_item = _sidebar_item(window, project_a.id)
    assert workspace.current_thread is not None
    assert workspace.current_thread.id == "created-a"
    assert workspace.current_thread.title == "create the real thread"
    assert all(
        project_item.child(index).text(0) != "New Thread (unsaved)"
        for index in range(project_item.childCount())
    )
    assert any(
        isinstance(project_item.child(index).data(0, Qt.ItemDataRole.UserRole), ThreadSummary)
        and project_item.child(index).data(0, Qt.ItemDataRole.UserRole).id == "created-a"
        for index in range(project_item.childCount())
    )


def test_generated_thread_title_survives_history_refresh_without_copying_body(qapp, tmp_path):
    workspace, project_a, _project_b = _workspace(tmp_path)
    generated = ThreadSummary(
        "generated-title",
        "openai",
        "gpt-5",
        "Inspect the failing test and fix it…",
        str(project_a.path),
        project_id=project_a.id,
    )
    workspace.registry.upsert_thread(project_a.id, generated)
    workspace.activate_project(project_a.id, source="title-refresh")
    workspace._on_router_history(
        [
            ThreadSummary(
                generated.id,
                generated.provider,
                generated.model,
                None,
                generated.cwd,
                generated.updated_at,
            )
        ]
    )

    assert workspace.registry.list_threads(project_a.id)[0].title == generated.title


def test_selecting_nested_thread_activates_its_project_before_resume(qapp, tmp_path):
    workspace, project_a, project_b = _workspace(tmp_path)
    summary = ThreadSummary(
        "nested-a",
        "openai",
        "gpt-5",
        "Nested A",
        str(project_a.path),
        project_id=project_a.id,
    )
    workspace.registry.upsert_thread(project_a.id, summary)
    window = MainWindow(
        workspace,
        settings=QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat),
    )
    project_item = _sidebar_item(window, project_a.id)
    project_item.setExpanded(True)
    thread_item = project_item.child(0)
    backend = workspace.backends["openai"]
    backend._ready = True

    def request(method, params=None, callback=None):
        if method == "thread/list":
            callback({"data": []}, None)
        else:
            callback(
                {
                    "thread": {
                        "id": "nested-a",
                        "modelProvider": "openai",
                        "cwd": str(project_a.path),
                    },
                    "model": "gpt-5",
                },
                None,
            )
        return 1

    backend.request = request
    window.project_tree.setCurrentItem(thread_item)
    qapp.processEvents()

    assert workspace.active_project_id == project_a.id
    assert workspace.current_thread is not None
    assert workspace.current_thread.id == "nested-a"
    assert _sidebar_project_id(window) == project_a.id
    assert window.project_tree.currentItem().parent().data(0, Qt.ItemDataRole.UserRole) == project_a.id
