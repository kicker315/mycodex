from __future__ import annotations

from mint_codex.models.session import ThreadSummary
from mint_codex.workspace.controller import WorkspaceController
from mint_codex.workspace.registry import ProjectRegistry


def test_project_registry_persists_projects_and_thread_index_without_body(tmp_path):
    database = tmp_path / "workspace.sqlite3"
    project_path = tmp_path / "rag-app"
    project_path.mkdir()
    registry = ProjectRegistry(database)

    project = registry.add_project(project_path, name="rag-app")
    registry.set_pinned(project.id, True)
    registry.open_project(project.id)
    registry.replace_threads(
        project.id,
        [ThreadSummary("thread-1", "openai", "gpt-5", "Agent refactor", str(project_path), "2026-08-08T10:00:00Z")],
    )

    restored = ProjectRegistry(database)
    saved_project = restored.get_project(project.id)
    saved_threads = restored.list_threads(project.id)

    assert saved_project is not None
    assert saved_project.name == "rag-app"
    assert saved_project.path == project_path.resolve()
    assert saved_project.pinned is True
    assert saved_threads == [
        ThreadSummary(
            "thread-1",
            "openai",
            "gpt-5",
            "Agent refactor",
            str(project_path),
            "2026-08-08T10:00:00Z",
            project.id,
        )
    ]


def test_project_registry_removes_project_without_touching_project_directory(tmp_path):
    database = tmp_path / "workspace.sqlite3"
    project_path = tmp_path / "rag-app"
    project_path.mkdir()
    registry = ProjectRegistry(database)
    project = registry.add_project(project_path)

    registry.remove_project(project.id)

    assert registry.get_project(project.id) is None
    assert project_path.is_dir()


def test_workspace_restores_last_project_and_requires_project_cwd(tmp_path):
    database = tmp_path / "workspace.sqlite3"
    project_path = tmp_path / "rag-app"
    project_path.mkdir()
    first = WorkspaceController(registry=ProjectRegistry(database), cwd=tmp_path)
    project = first.add_project(project_path)
    first.registry.set_last_thread(project.id, "thread-1", "openai")

    restored = WorkspaceController(registry=ProjectRegistry(database), cwd=tmp_path)

    assert restored.current_project is not None
    assert restored.current_project.id == project.id
    assert restored.cwd == project_path.resolve()
    assert restored.registry.last_thread(project.id) == ("thread-1", "openai")
