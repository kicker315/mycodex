from __future__ import annotations

import sqlite3
import uuid
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from mint_codex.models.session import ThreadSummary
from mint_codex.models.agent_task import AgentTask, AgentTaskStatus
from mint_codex.models.orchestration import (
    OrchestrationNode,
    OrchestrationNodeStatus,
    OrchestrationRun,
    OrchestrationRunStatus,
    ResultEnvelope,
)


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
    """Persist Project metadata, lightweight Thread indexes, and Task metadata.

    Codex remains the source of truth for Thread, Turn, and Item contents.
    This registry stores enough metadata to group server-owned Threads under a
    local Project, restore workspace selection, and recover isolated Agent
    Task worktrees without copying conversation bodies.
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
            CREATE TABLE IF NOT EXISTS agent_tasks (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                task_prompt TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                model_id TEXT,
                reasoning_effort TEXT,
                thread_id TEXT,
                worktree_path TEXT NOT NULL,
                branch_name TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completion_metadata TEXT NOT NULL DEFAULT '{}',
                active_turn_id TEXT,
                base_commit TEXT
            );
            CREATE INDEX IF NOT EXISTS agent_tasks_by_project_activity
                ON agent_tasks(project_id, status, updated_at DESC);
            CREATE TABLE IF NOT EXISTS workspace_state (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS orchestration_runs (
                run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                objective TEXT NOT NULL,
                plan_version INTEGER NOT NULL,
                base_commit TEXT NOT NULL,
                default_provider_id TEXT NOT NULL,
                default_model_id TEXT,
                max_parallelism INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                approved_at TEXT,
                started_at TEXT,
                completed_at TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS orchestration_nodes (
                node_id TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES orchestration_runs(run_id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                instructions TEXT NOT NULL,
                dependencies TEXT NOT NULL DEFAULT '[]',
                provider_id TEXT,
                model_id TEXT,
                capability_hint TEXT,
                status TEXT NOT NULL,
                agent_task_id TEXT,
                result TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                blocked_reason TEXT,
                PRIMARY KEY (run_id, node_id)
            );
            CREATE INDEX IF NOT EXISTS orchestration_runs_by_project
                ON orchestration_runs(project_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS orchestration_nodes_by_run
                ON orchestration_nodes(run_id, node_id);
            """
        )
        self._ensure_column("agent_tasks", "base_commit", "TEXT")
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

    def get_active_workspace_task_id(self) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM workspace_state WHERE key = 'active_workspace_task_id'"
        ).fetchone()
        task_id = row["value"] if row else None
        if not isinstance(task_id, str):
            return None
        task = self.get_agent_task(task_id)
        return task.id if task else None

    def set_active_workspace_task_id(self, task_id: str | None) -> None:
        if task_id is None:
            self._connection.execute("DELETE FROM workspace_state WHERE key = 'active_workspace_task_id'")
        else:
            if self.get_agent_task(task_id) is None:
                raise KeyError(f"Unknown Agent Task: {task_id}")
            self._connection.execute(
                "INSERT INTO workspace_state(key, value) VALUES ('active_workspace_task_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (task_id,),
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

    def get_agent_task(self, task_id: str) -> AgentTask | None:
        row = self._connection.execute("SELECT * FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()
        return self._agent_task_from_row(row) if row else None

    def list_agent_tasks(self, project_id: str, *, include_archived: bool = False) -> list[AgentTask]:
        query = "SELECT * FROM agent_tasks WHERE project_id = ?"
        params: list[object] = [project_id]
        if not include_archived:
            query += " AND status != ?"
            params.append(AgentTaskStatus.ARCHIVED.value)
        query += " ORDER BY updated_at DESC, id"
        rows = self._connection.execute(query, params).fetchall()
        return [self._agent_task_from_row(row) for row in rows]

    def upsert_agent_task(self, task: AgentTask) -> None:
        if self.get_project(task.project_id) is None:
            raise KeyError(f"Unknown Project: {task.project_id}")
        self._connection.execute(
            """
            INSERT INTO agent_tasks(
                id, project_id, title, task_prompt, provider_id, model_id,
                reasoning_effort, thread_id, worktree_path, branch_name, status,
                created_at, updated_at, completion_metadata, active_turn_id, base_commit
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                project_id = excluded.project_id,
                title = excluded.title,
                task_prompt = excluded.task_prompt,
                provider_id = excluded.provider_id,
                model_id = excluded.model_id,
                reasoning_effort = excluded.reasoning_effort,
                thread_id = excluded.thread_id,
                worktree_path = excluded.worktree_path,
                branch_name = excluded.branch_name,
                status = excluded.status,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                completion_metadata = excluded.completion_metadata,
                active_turn_id = excluded.active_turn_id,
                base_commit = excluded.base_commit
            """,
            (
                task.id,
                task.project_id,
                task.title,
                task.task_prompt,
                task.provider_id,
                task.model_id,
                task.reasoning_effort,
                task.thread_id,
                str(task.worktree_path),
                task.branch_name,
                task.status.value,
                task.created_at,
                task.updated_at,
                json.dumps(task.completion_metadata, ensure_ascii=False, sort_keys=True),
                task.active_turn_id,
                task.base_commit,
            ),
        )
        self._connection.commit()

    def upsert_orchestration(self, run: OrchestrationRun, nodes: Iterable[OrchestrationNode]) -> None:
        """Persist one Run and its complete Plan atomically."""

        if self.get_project(run.project_id) is None:
            raise KeyError(f"Unknown Project: {run.project_id}")
        normalized = list(nodes)
        if any(node.run_id != run.run_id for node in normalized):
            raise ValueError("Orchestration Node belongs to a different Run")
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO orchestration_runs(
                    run_id, project_id, objective, plan_version, base_commit,
                    default_provider_id, default_model_id, max_parallelism,
                    status, created_at, approved_at, started_at, completed_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    project_id = excluded.project_id,
                    objective = excluded.objective,
                    plan_version = excluded.plan_version,
                    base_commit = excluded.base_commit,
                    default_provider_id = excluded.default_provider_id,
                    default_model_id = excluded.default_model_id,
                    max_parallelism = excluded.max_parallelism,
                    status = excluded.status,
                    created_at = excluded.created_at,
                    approved_at = excluded.approved_at,
                    started_at = excluded.started_at,
                    completed_at = excluded.completed_at,
                    metadata = excluded.metadata
                """,
                (
                    run.run_id,
                    run.project_id,
                    run.objective,
                    run.plan_version,
                    run.base_commit,
                    run.default_provider_id,
                    run.default_model_id,
                    run.max_parallelism,
                    run.status.value,
                    run.created_at,
                    run.approved_at,
                    run.started_at,
                    run.completed_at,
                    json.dumps(run.metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
            self._connection.execute("DELETE FROM orchestration_nodes WHERE run_id = ?", (run.run_id,))
            self._connection.executemany(
                """
                INSERT INTO orchestration_nodes(
                    node_id, run_id, title, instructions, dependencies, provider_id,
                    model_id, capability_hint, status, agent_task_id, result,
                    created_at, updated_at, blocked_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        node.node_id,
                        node.run_id,
                        node.title,
                        node.instructions,
                        json.dumps(list(node.dependencies), ensure_ascii=False),
                        node.provider_id,
                        node.model_id,
                        node.capability_hint,
                        node.status.value,
                        node.agent_task_id,
                        json.dumps(node.result.to_dict(), ensure_ascii=False, sort_keys=True)
                        if node.result is not None
                        else None,
                        node.created_at,
                        node.updated_at,
                        node.blocked_reason,
                    )
                    for node in normalized
                ],
            )

    def get_orchestration(self, run_id: str) -> tuple[OrchestrationRun | None, list[OrchestrationNode]]:
        row = self._connection.execute(
            "SELECT * FROM orchestration_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None, []
        nodes = self._connection.execute(
            "SELECT * FROM orchestration_nodes WHERE run_id = ? ORDER BY node_id", (run_id,)
        ).fetchall()
        return self._orchestration_run_from_row(row), [self._orchestration_node_from_row(item) for item in nodes]

    def list_orchestrations(self, project_id: str) -> list[OrchestrationRun]:
        rows = self._connection.execute(
            "SELECT * FROM orchestration_runs WHERE project_id = ? ORDER BY created_at DESC, run_id",
            (project_id,),
        ).fetchall()
        return [self._orchestration_run_from_row(row) for row in rows]

    def remove_agent_task(self, task_id: str) -> None:
        self._connection.execute("DELETE FROM agent_tasks WHERE id = ?", (task_id,))
        self._connection.execute(
            "DELETE FROM workspace_state WHERE key = 'active_workspace_task_id' AND value = ?", (task_id,)
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

    @staticmethod
    def _agent_task_from_row(row: sqlite3.Row) -> AgentTask:
        try:
            status = AgentTaskStatus(row["status"])
        except ValueError:
            status = AgentTaskStatus.ORPHANED
        try:
            metadata = json.loads(row["completion_metadata"] or "{}")
        except (TypeError, ValueError):
            metadata = {"recovery_error": "Task metadata was not valid JSON"}
        if not isinstance(metadata, dict):
            metadata = {"recovery_error": "Task metadata was not an object"}
        return AgentTask(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            task_prompt=row["task_prompt"],
            provider_id=row["provider_id"],
            model_id=row["model_id"],
            reasoning_effort=row["reasoning_effort"],
            thread_id=row["thread_id"],
            worktree_path=Path(row["worktree_path"]),
            branch_name=row["branch_name"],
            status=status,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completion_metadata=metadata,
            active_turn_id=row["active_turn_id"],
            base_commit=row["base_commit"],
        )

    @staticmethod
    def _orchestration_run_from_row(row: sqlite3.Row) -> OrchestrationRun:
        try:
            status = OrchestrationRunStatus(row["status"])
        except ValueError:
            status = OrchestrationRunStatus.RECOVERY_REQUIRED
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            metadata = {"recovery_error": "Run metadata was not valid JSON"}
        if not isinstance(metadata, dict):
            metadata = {"recovery_error": "Run metadata was not an object"}
        return OrchestrationRun(
            run_id=row["run_id"],
            project_id=row["project_id"],
            objective=row["objective"],
            base_commit=row["base_commit"],
            default_provider_id=row["default_provider_id"],
            default_model_id=row["default_model_id"],
            max_parallelism=row["max_parallelism"],
            plan_version=row["plan_version"],
            status=status,
            created_at=row["created_at"],
            approved_at=row["approved_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            metadata=metadata,
        )

    @staticmethod
    def _orchestration_node_from_row(row: sqlite3.Row) -> OrchestrationNode:
        try:
            status = OrchestrationNodeStatus(row["status"])
        except ValueError:
            status = OrchestrationNodeStatus.BLOCKED
        try:
            dependencies = json.loads(row["dependencies"] or "[]")
        except (TypeError, ValueError):
            dependencies = []
        if not isinstance(dependencies, list):
            dependencies = []
        try:
            result_payload = json.loads(row["result"]) if row["result"] else None
        except (TypeError, ValueError):
            result_payload = None
        return OrchestrationNode(
            node_id=row["node_id"],
            run_id=row["run_id"],
            title=row["title"],
            instructions=row["instructions"],
            dependencies=tuple(value for value in dependencies if isinstance(value, str)),
            provider_id=row["provider_id"],
            model_id=row["model_id"],
            capability_hint=row["capability_hint"],
            status=status,
            agent_task_id=row["agent_task_id"],
            result=ResultEnvelope.from_dict(result_payload),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            blocked_reason=row["blocked_reason"],
        )

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in self._connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _now() -> str:
    # Project activation order is also the restore order.  Second-level
    # timestamps make two quick activations nondeterministically tie.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
