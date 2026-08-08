from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import QProcess, QTimer, QObject, Signal

from .file_access import FileAccessError, resolve_project_path


class GitChangeKind(StrEnum):
    MODIFIED = "modified"
    ADDED = "added"
    DELETED = "deleted"
    RENAMED = "renamed"
    UNTRACKED = "untracked"


@dataclass(frozen=True)
class GitChange:
    path: str
    kind: GitChangeKind
    index_status: str = " "
    worktree_status: str = " "
    old_path: str | None = None

    @property
    def is_staged(self) -> bool:
        return self.index_status not in {" ", "?"}

    @property
    def is_unstaged(self) -> bool:
        return self.worktree_status not in {" "}

    @property
    def label(self) -> str:
        prefix = {
            GitChangeKind.MODIFIED: "M",
            GitChangeKind.ADDED: "A",
            GitChangeKind.DELETED: "D",
            GitChangeKind.RENAMED: "R",
            GitChangeKind.UNTRACKED: "?",
        }[self.kind]
        if self.kind == GitChangeKind.UNTRACKED:
            return f"{prefix} {self.path}"
        if self.is_staged and self.is_unstaged:
            stage = "staged+unstaged"
        elif self.is_staged:
            stage = "staged"
        else:
            stage = "unstaged"
        return f"{prefix} [{stage}] {self.path}"


@dataclass(frozen=True)
class GitStatusSnapshot:
    root: Path | None
    is_git: bool
    changes: tuple[GitChange, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class GitDiffResult:
    root: Path | None
    path: str
    text: str = ""
    is_binary: bool = False
    too_large: bool = False
    error: str | None = None


def parse_git_status(payload: bytes) -> list[GitChange]:
    """Parse ``git status --porcelain=v1 -z`` without depending on locale."""

    tokens = payload.split(b"\0")
    changes: list[GitChange] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        try:
            record = token.decode("utf-8", "surrogateescape")
        except UnicodeDecodeError:
            continue
        if len(record) < 3:
            continue
        index_status, worktree_status = record[0], record[1]
        path = record[3:]
        old_path = None
        if "R" in {index_status, worktree_status} or "C" in {index_status, worktree_status}:
            if index < len(tokens) and tokens[index]:
                old_path = tokens[index].decode("utf-8", "surrogateescape")
                index += 1
        if index_status == "?" and worktree_status == "?":
            kind = GitChangeKind.UNTRACKED
        elif "R" in {index_status, worktree_status}:
            kind = GitChangeKind.RENAMED
        elif "A" in {index_status, worktree_status}:
            kind = GitChangeKind.ADDED
        elif "D" in {index_status, worktree_status}:
            kind = GitChangeKind.DELETED
        elif any(value in {"M", "T", "U", "C"} for value in (index_status, worktree_status)):
            kind = GitChangeKind.MODIFIED
        else:
            continue
        changes.append(GitChange(path, kind, index_status, worktree_status, old_path))
    return changes


class GitWorkspace(QObject):
    """Asynchronous Git observation scoped to one authoritative Project root."""

    status_changed = Signal(object)
    diff_changed = Signal(object)
    error = Signal(str)
    busy_changed = Signal(bool)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        timeout_ms: int = 5000,
        max_diff_bytes: int = 4 * 1024 * 1024,
    ):
        super().__init__(parent)
        self._root: Path | None = None
        self._generation = 0
        self._timeout_ms = timeout_ms
        self._max_diff_bytes = max_diff_bytes
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._process.finished.connect(self._finished)
        self._process.errorOccurred.connect(self._process_error)
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._operation: str | None = None
        self._operation_generation = 0
        self._operation_path = ""
        self._operation_is_untracked = False
        self._too_large = False
        self._refresh_pending = False
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._timed_out)

        self._process.readyReadStandardOutput.connect(self._read_stdout)
        self._process.readyReadStandardError.connect(self._read_stderr)

    @property
    def root(self) -> Path | None:
        return self._root

    @property
    def busy(self) -> bool:
        return self._operation is not None

    def set_root(self, root: Path | None) -> None:
        self._generation += 1
        self._stop_process()
        self._root = root.expanduser().resolve(strict=False) if root is not None else None
        self._operation = None
        self._refresh_pending = False

    def refresh(self) -> None:
        if self._operation is not None:
            self._refresh_pending = True
            return
        if self._root is None:
            self.status_changed.emit(GitStatusSnapshot(None, False, message="No active Project"))
            return
        if not self._root.is_dir():
            self.status_changed.emit(GitStatusSnapshot(self._root, False, message="Project path unavailable"))
            return
        self._start_process("status", ["status", "--porcelain=v1", "-z", "--untracked-files=all"])

    def diff_for(self, path: str, change: GitChange | None = None) -> None:
        if self._operation is not None:
            self._refresh_pending = True
            return
        if self._root is None:
            self.diff_changed.emit(GitDiffResult(None, path, error="No active Project"))
            return
        try:
            resolved = resolve_project_path(self._root, path, must_exist=False)
        except FileAccessError as exc:
            self.diff_changed.emit(GitDiffResult(self._root, path, error=str(exc)))
            return
        is_untracked = change is not None and change.kind == GitChangeKind.UNTRACKED
        if is_untracked:
            args = ["diff", "--no-index", "--no-color", "--unified=3", "--", "/dev/null", str(resolved)]
        else:
            args = ["diff", "--no-color", "--unified=3", "HEAD", "--", path]
        self._start_process("diff", args, path=path, is_untracked=is_untracked)

    def close(self) -> None:
        self._stop_process()

    def _start_process(self, operation: str, args: list[str], *, path: str = "", is_untracked: bool = False) -> None:
        if self._root is None:
            return
        self._stdout.clear()
        self._stderr.clear()
        self._too_large = False
        self._operation = operation
        self._operation_generation = self._generation
        self._operation_path = path
        self._operation_is_untracked = is_untracked
        self._process.setWorkingDirectory(str(self._root))
        self._process.start("git", args)
        self._timeout.start(self._timeout_ms)
        self.busy_changed.emit(True)

    def _read_stdout(self) -> None:
        chunk = bytes(self._process.readAllStandardOutput())
        max_bytes = self._max_diff_bytes if self._operation == "diff" else 4 * 1024 * 1024
        if len(self._stdout) + len(chunk) > max_bytes:
            self._too_large = True
            self._stdout.clear()
            self._stop_process(force=True)
            return
        self._stdout.extend(chunk)

    def _read_stderr(self) -> None:
        self._stderr.extend(bytes(self._process.readAllStandardError()))

    def _finished(self, code: int, _status: QProcess.ExitStatus) -> None:
        operation = self._operation
        generation = self._operation_generation
        path = self._operation_path
        stderr = bytes(self._stderr).decode("utf-8", "replace").strip()
        stdout = bytes(self._stdout)
        too_large = self._too_large
        self._timeout.stop()
        self._operation = None
        self._stdout.clear()
        self._stderr.clear()
        self._too_large = False
        if operation is None:
            return
        if generation != self._generation:
            self._complete_pending()
            return
        if operation == "status":
            if too_large:
                snapshot = GitStatusSnapshot(self._root, False, message="Git status output is too large")
            elif code == 0:
                snapshot = GitStatusSnapshot(self._root, True, tuple(parse_git_status(stdout)), "")
            elif "not a git repository" in stderr.lower():
                snapshot = GitStatusSnapshot(self._root, False, message="Not a Git repository")
            else:
                snapshot = GitStatusSnapshot(self._root, False, message=stderr or f"git status failed ({code})")
            self.status_changed.emit(snapshot)
        else:
            if too_large:
                result = GitDiffResult(self._root, path, too_large=True, error="Diff is too large to preview")
            elif b"Binary files" in stdout or b"GIT binary patch" in stdout:
                result = GitDiffResult(self._root, path, is_binary=True, error="Binary file cannot be previewed")
            elif code in {0, 1}:
                result = GitDiffResult(self._root, path, stdout.decode("utf-8", "replace"))
            elif "ambiguous argument 'HEAD'" in stderr and not self._operation_is_untracked:
                self._start_process(
                    "diff",
                    ["diff", "--no-index", "--no-color", "--unified=3", "--", "/dev/null", str(self._root / path)],
                    path=path,
                    is_untracked=True,
                )
                return
            else:
                result = GitDiffResult(self._root, path, error=stderr or f"git diff failed ({code})")
            self.diff_changed.emit(result)
        self.busy_changed.emit(False)
        self._complete_pending()

    def _complete_pending(self) -> None:
        if self._refresh_pending:
            self._refresh_pending = False
            self.refresh()

    def _process_error(self, _error: QProcess.ProcessError) -> None:
        if self._operation is not None:
            self.error.emit(self._process.errorString())

    def _timed_out(self) -> None:
        if self._operation is None:
            return
        operation = self._operation
        path = self._operation_path
        self._operation = None
        self._stop_process(force=True)
        self.busy_changed.emit(False)
        if operation == "status":
            self.status_changed.emit(GitStatusSnapshot(self._root, False, message="Git status timed out"))
        else:
            self.diff_changed.emit(GitDiffResult(self._root, path, error="Git diff timed out"))
        self._complete_pending()

    def _stop_process(self, *, force: bool = False) -> None:
        self._timeout.stop()
        if self._process.state() == QProcess.ProcessState.NotRunning:
            return
        if force:
            self._process.kill()
        else:
            self._process.kill()
        self._process.waitForFinished(100)
