from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class WorktreeError(RuntimeError):
    """A safe, user-facing failure from Git worktree management."""


class DirtyWorktreeError(WorktreeError):
    pass


@dataclass(frozen=True)
class WorktreeInspection:
    path: Path
    branch_name: str
    exists: bool
    registered: bool
    branch_exists: bool
    dirty: bool
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.exists and self.registered and self.branch_exists and self.error is None


class WorktreeManager:
    """Deep Git worktree lifecycle module for Agent Tasks.

    All Git commands receive an argv list and a validated cwd/path.  The
    managed root is the seam that makes ownership and cleanup auditable.
    """

    _SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

    def __init__(self, managed_root: Path | None = None):
        self.managed_root = (managed_root or Path.home() / ".local" / "share" / "mint-codex" / "worktrees").expanduser().resolve()

    def validate_repository(self, project_root: Path) -> Path:
        root = project_root.expanduser().resolve(strict=False)
        if not root.is_dir():
            raise WorktreeError(f"Project path is unavailable: {root}")
        result = self._git(root, ["rev-parse", "--show-toplevel"])
        if result.returncode != 0:
            raise WorktreeError("Agent Tasks require a Git repository")
        try:
            repository_root = Path(result.stdout.strip()).resolve()
        except OSError as exc:
            raise WorktreeError("Git repository root could not be resolved") from exc
        if repository_root != root:
            raise WorktreeError("Project must be the Git repository root for Agent Tasks")
        return repository_root

    def task_path(self, project_id: str, task_id: str) -> Path:
        self._validate_segment(project_id, "project id")
        self._validate_segment(task_id, "task id")
        path = (self.managed_root / project_id / task_id).resolve(strict=False)
        self._ensure_inside(path, self.managed_root)
        return path

    def branch_name(self, project_id: str, task_id: str) -> str:
        self._validate_segment(project_id, "project id")
        self._validate_segment(task_id, "task id")
        branch = f"mint-codex/{project_id}/{task_id}"
        if not self._valid_git_ref(branch):
            raise WorktreeError("Generated Agent Task branch is not a valid Git ref")
        return branch

    def create(self, project_root: Path, project_id: str, task_id: str) -> tuple[Path, str]:
        root = self.validate_repository(project_root)
        path = self.task_path(project_id, task_id)
        branch = self.branch_name(project_id, task_id)
        if path.exists():
            raise WorktreeError(f"Agent Task worktree already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        result = self._git(root, ["worktree", "add", "-b", branch, str(path), "HEAD"])
        if result.returncode != 0:
            self._remove_empty_directory(path)
            message = result.stderr.strip() or "git worktree add failed"
            raise WorktreeError(message)
        inspection = self.inspect(root, project_id, task_id, branch)
        if not inspection.usable:
            raise WorktreeError(inspection.error or "Created worktree failed validation")
        return path, branch

    def inspect(self, project_root: Path, project_id: str, task_id: str, branch_name: str | None = None) -> WorktreeInspection:
        path = self.task_path(project_id, task_id)
        branch = branch_name or self.branch_name(project_id, task_id)
        try:
            root = self.validate_repository(project_root)
        except WorktreeError as exc:
            return WorktreeInspection(path, branch, path.is_dir(), False, False, False, str(exc))

        registered = False
        worktree_output = self._git(root, ["worktree", "list", "--porcelain"])
        if worktree_output.returncode == 0:
            registered = _worktree_is_registered(worktree_output.stdout, path)
        branch_result = self._git(root, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
        branch_exists = branch_result.returncode == 0
        dirty = False
        if path.is_dir():
            status = self._git(path, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
            dirty = bool(status.stdout)
        error = None
        if worktree_output.returncode != 0:
            error = worktree_output.stderr.strip() or "Could not inspect Git worktrees"
        return WorktreeInspection(path, branch, path.is_dir(), registered, branch_exists, dirty, error)

    def cleanup(self, project_root: Path, project_id: str, task_id: str, branch_name: str) -> None:
        root = self.validate_repository(project_root)
        path = self.task_path(project_id, task_id)
        expected_branch = self.branch_name(project_id, task_id)
        if branch_name != expected_branch:
            raise WorktreeError("Worktree branch does not match the Agent Task")
        if not path.exists():
            self._git(root, ["worktree", "prune"])
            return
        inspection = self.inspect(root, project_id, task_id, branch_name)
        if not inspection.registered:
            raise WorktreeError("Worktree is not registered under the managed Git repository")
        if inspection.dirty:
            raise DirtyWorktreeError("Worktree has uncommitted changes; cleanup was refused")
        result = self._git(root, ["worktree", "remove", "--", str(path)])
        if result.returncode != 0:
            raise WorktreeError(result.stderr.strip() or "git worktree remove failed")
        prune = self._git(root, ["worktree", "prune"])
        if prune.returncode != 0:
            raise WorktreeError(prune.stderr.strip() or "git worktree prune failed")

    @classmethod
    def _validate_segment(cls, value: str, label: str) -> None:
        if not isinstance(value, str) or not cls._SAFE_SEGMENT.fullmatch(value):
            raise WorktreeError(f"Invalid {label}")

    @staticmethod
    def _valid_git_ref(value: str) -> bool:
        result = subprocess.run(
            ["git", "check-ref-format", value],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return result.returncode == 0

    @staticmethod
    def _ensure_inside(path: Path, root: Path) -> None:
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise WorktreeError("Worktree path escaped the managed directory") from exc

    @staticmethod
    def _git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    @staticmethod
    def _remove_empty_directory(path: Path) -> None:
        try:
            path.rmdir()
        except OSError:
            pass


def _worktree_is_registered(payload: str, expected: Path) -> bool:
    current: Path | None = None
    for line in payload.splitlines():
        if line.startswith("worktree "):
            current = Path(line[9:]).expanduser().resolve(strict=False)
        elif line == "" and current is not None:
            if current == expected.resolve(strict=False):
                return True
            current = None
    return current == expected.resolve(strict=False)
