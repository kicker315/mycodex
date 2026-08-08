from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from mint_codex.models.session import ThreadSummary


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    path: Path
    last_opened_at: str | None = None
    pinned: bool = False
    default_provider: str | None = None
    default_model: str | None = None


class ProjectRegistry:
    """Persist Project metadata and lightweight Thread indexes only.

    Codex remains the source of truth for Thread, Turn, and Item contents.
    This registry stores enough metadata to group server-owned Threads under a
    local Project and restore the last workspace selection.
    """

    def __init__(self, database_path: Path | None = None):
        self.database_path = (
            database_path or Path.home() / ".local" / "share" / "mint-codex" / "workspace.sqlite3"
        ).expanduser()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                last_opened_at TEXT,
                pinned INTEGER NOT NULL DEFAULT 0,
                default_provider TEXT,
                default_model TEXT,
                last_thread_id TEXT,
                last_thread_provider TEXT
            );
            CREATE TABLE IF NOT EXISTS threads (
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                thread_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT,
                title TEXT,
                cwd TEXT,
                updated_at TEXT,
                archived INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (project_id, thread_id)
            );
            CREATE INDEX IF NOT EXISTS threads_by_project_activity
                ON threads(project_id, archived, updated_at DESC);
            CREATE TABLE IF NOT EXISTS workspace_state (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def add_project(
        self,
        path: Path,
        name: str | None = None,
        default_provider: str | None = None,
        default_model: str | None = None,
    ) -> Project:
        absolute_path = path.expanduser().resolve()
        if not absolute_path.is_dir():
            raise ValueError(f"Project directory does not exist: {absolute_path}")
        existing = self._connection.execute("SELECT id FROM projects WHERE path = ?", (str(absolute_path),)).fetchone()
        if existing:
            project = self.get_project(existing["id"])
            if project is None:
                raise RuntimeError("Project registry returned an invalid project")
            return project
        project = Project(
            id=str(uuid.uuid4()),
            name=(name or absolute_path.name or str(absolute_path)).strip(),
            path=absolute_path,
            default_provider=default_provider,
            default_model=default_model,
        )
        self._connection.execute(
            """
            INSERT INTO projects(id, name, path, default_provider, default_model)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project.id, project.name, str(project.path), project.default_provider, project.default_model),
        )
        self._connection.commit()
        return project

    def get_project(self, project_id: str) -> Project | None:
        row = self._connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return self._project_from_row(row) if row else None

    def get_project_by_path(self, path: Path) -> Project | None:
        absolute_path = path.expanduser().resolve()
        row = self._connection.execute("SELECT * FROM projects WHERE path = ?", (str(absolute_path),)).fetchone()
        return self._project_from_row(row) if row else None

    def get_active_project_id(self) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM workspace_state WHERE key = 'active_project_id'"
        ).fetchone()
        project_id = row["value"] if row else None
        return project_id if isinstance(project_id, str) and self.get_project(project_id) else None

    def set_active_project_id(self, project_id: str | None) -> None:
        if project_id is not None and self.get_project(project_id) is None:
            raise KeyError(f"Unknown project: {project_id}")
        if project_id is None:
            self._connection.execute("DELETE FROM workspace_state WHERE key = 'active_project_id'")
        else:
            self._connection.execute(
                "INSERT INTO workspace_state(key, value) VALUES ('active_project_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (project_id,),
            )
        self._connection.commit()

    def list_projects(self) -> list[Project]:
        rows = self._connection.execute(
            "SELECT * FROM projects ORDER BY pinned DESC, last_opened_at DESC, name COLLATE NOCASE"
        ).fetchall()
        return [self._project_from_row(row) for row in rows]

    def open_project(self, project_id: str) -> Project:
        timestamp = _now()
        cursor = self._connection.execute(
            "UPDATE projects SET last_opened_at = ? WHERE id = ?", (timestamp, project_id)
        )
        if cursor.rowcount != 1:
            raise KeyError(f"Unknown project: {project_id}")
        self._connection.commit()
        project = self.get_project(project_id)
        if project is None:
            raise RuntimeError("Project disappeared from registry")
        return project

    def remove_project(self, project_id: str) -> None:
        cursor = self._connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown project: {project_id}")
        self._connection.commit()

    def set_pinned(self, project_id: str, pinned: bool) -> Project:
        cursor = self._connection.execute("UPDATE projects SET pinned = ? WHERE id = ?", (int(pinned), project_id))
        if cursor.rowcount != 1:
            raise KeyError(f"Unknown project: {project_id}")
        self._connection.commit()
        project = self.get_project(project_id)
        if project is None:
            raise RuntimeError("Project disappeared from registry")
        return project

    def set_last_thread(self, project_id: str, thread_id: str | None, provider: str | None = None) -> None:
        cursor = self._connection.execute(
            "UPDATE projects SET last_thread_id = ?, last_thread_provider = ? WHERE id = ?",
            (thread_id, provider, project_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"Unknown project: {project_id}")
        self._connection.commit()

    def last_thread(self, project_id: str) -> tuple[str, str] | None:
        row = self._connection.execute(
            "SELECT last_thread_id, last_thread_provider FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not row or not row["last_thread_id"] or not row["last_thread_provider"]:
            return None
        return row["last_thread_id"], row["last_thread_provider"]

    def replace_threads(self, project_id: str, summaries: Iterable[ThreadSummary]) -> None:
        if self.get_project(project_id) is None:
            raise KeyError(f"Unknown project: {project_id}")
        rows = list(summaries)
        with self._connection:
            self._connection.execute("DELETE FROM threads WHERE project_id = ?", (project_id,))
            self._connection.executemany(
                """
                INSERT INTO threads(project_id, thread_id, provider, model, title, cwd, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        project_id,
                        summary.id,
                        summary.provider,
                        summary.model,
                        summary.title,
                        summary.cwd,
                        summary.updated_at,
                    )
                    for summary in rows
                ],
            )

    def list_threads(self, project_id: str) -> list[ThreadSummary]:
        rows = self._connection.execute(
            """
            SELECT thread_id, provider, model, title, cwd, updated_at, project_id
            FROM threads
            WHERE project_id = ? AND archived = 0
            ORDER BY updated_at DESC, thread_id
            """,
            (project_id,),
        ).fetchall()
        return [
            ThreadSummary(
                row["thread_id"],
                row["provider"],
                row["model"],
                row["title"],
                row["cwd"],
                row["updated_at"],
                row["project_id"],
            )
            for row in rows
        ]

    def upsert_thread(self, project_id: str, summary: ThreadSummary) -> None:
        if self.get_project(project_id) is None:
            raise KeyError(f"Unknown project: {project_id}")
        self._connection.execute(
            """
            INSERT INTO threads(project_id, thread_id, provider, model, title, cwd, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, thread_id) DO UPDATE SET
                provider = excluded.provider,
                model = excluded.model,
                title = excluded.title,
                cwd = excluded.cwd,
                updated_at = excluded.updated_at,
                archived = 0
            """,
            (project_id, summary.id, summary.provider, summary.model, summary.title, summary.cwd, summary.updated_at),
        )
        self._connection.commit()

    def remove_thread(self, project_id: str, thread_id: str) -> None:
        self._connection.execute(
            "DELETE FROM threads WHERE project_id = ? AND thread_id = ?", (project_id, thread_id)
        )
        self._connection.commit()

    @staticmethod
    def _project_from_row(row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            path=Path(row["path"]),
            last_opened_at=row["last_opened_at"],
            pinned=bool(row["pinned"]),
            default_provider=row["default_provider"],
            default_model=row["default_model"],
        )


def _now() -> str:
    # Project activation order is also the restore order.  Second-level
    # timestamps make two quick activations nondeterministically tie.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
