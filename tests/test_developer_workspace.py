from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mint_codex.workspace.file_access import FileAccessError, read_project_file, resolve_project_path
from mint_codex.workspace.git_workspace import GitChangeKind, GitWorkspace, parse_git_status


def _wait_until(qapp, predicate, timeout=3000):
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    deadline.start(timeout)
    while not predicate() and deadline.isActive():
        QTimer.singleShot(20, loop.quit)
        loop.exec()
    deadline.stop()
    return predicate()


def _git_project(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "tracked.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "initial"], check=True)


def test_git_status_parser_handles_modified_added_deleted_renamed_and_untracked():
    payload = (
        b" M src/modified.py\0"
        b"A  src/added.py\0"
        b" D src/deleted.py\0"
        b"R  src/new_name.py\0src/old_name.py\0"
        b"?? notes.txt\0"
    )

    changes = parse_git_status(payload)

    assert [(change.kind, change.path) for change in changes] == [
        (GitChangeKind.MODIFIED, "src/modified.py"),
        (GitChangeKind.ADDED, "src/added.py"),
        (GitChangeKind.DELETED, "src/deleted.py"),
        (GitChangeKind.RENAMED, "src/new_name.py"),
        (GitChangeKind.UNTRACKED, "notes.txt"),
    ]
    assert changes[3].old_path == "src/old_name.py"
    assert changes[0].is_staged is False


def test_file_access_rejects_outside_paths_and_symlink_escape(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (project / "inside.txt").write_text("hello", encoding="utf-8")

    assert resolve_project_path(project, "inside.txt") == project / "inside.txt"
    with pytest.raises(FileAccessError):
        resolve_project_path(project, "../outside.txt")

    link = project / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(FileAccessError):
        read_project_file(project, "link.txt")


def test_file_preview_protects_binary_and_large_files(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "binary.bin").write_bytes(b"\x00\x01\x02")
    (project / "large.txt").write_bytes(b"x" * 32)

    with pytest.raises(FileAccessError, match="Binary"):
        read_project_file(project, "binary.bin")
    with pytest.raises(FileAccessError, match="large"):
        read_project_file(project, "large.txt", max_bytes=16)


def test_file_preview_reports_encoding_errors(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "invalid.txt").write_bytes(b"\xff\xfe")

    with pytest.raises(FileAccessError, match="encoding"):
        read_project_file(project, "invalid.txt")


def test_file_preview_reports_missing_file(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(FileAccessError, match="does not exist"):
        read_project_file(project, "missing.py")


def test_git_workspace_reads_real_status_and_diff_without_blocking(qapp, tmp_path: Path):
    project = tmp_path / "repo"
    project.mkdir()
    _git_project(project)
    (project / "tracked.txt").write_text("after\n", encoding="utf-8")
    (project / "new.txt").write_text("new\n", encoding="utf-8")
    workspace = GitWorkspace(timeout_ms=3000)
    snapshots = []
    diffs = []
    workspace.status_changed.connect(snapshots.append)
    workspace.diff_changed.connect(diffs.append)
    workspace.set_root(project)
    workspace.refresh()

    assert _wait_until(qapp, lambda: bool(snapshots))
    snapshot = snapshots[-1]
    assert snapshot.is_git is True
    assert {change.path for change in snapshot.changes} == {"tracked.txt", "new.txt"}
    workspace.diff_for("tracked.txt", next(change for change in snapshot.changes if change.path == "tracked.txt"))
    assert _wait_until(qapp, lambda: bool(diffs))
    assert "-before" in diffs[-1].text
    assert "+after" in diffs[-1].text
    workspace.close()


def test_git_workspace_non_git_project_is_a_normal_state(tmp_path: Path, qapp):
    project = tmp_path / "plain"
    project.mkdir()
    workspace = GitWorkspace(timeout_ms=3000)
    snapshots = []
    workspace.status_changed.connect(snapshots.append)
    workspace.set_root(project)
    workspace.refresh()

    assert _wait_until(qapp, lambda: bool(snapshots))
    assert snapshots[-1].is_git is False
    assert snapshots[-1].message == "Not a Git repository"


def test_git_diff_covers_empty_added_deleted_and_untracked_files(qapp, tmp_path: Path):
    project = tmp_path / "repo"
    project.mkdir()
    _git_project(project)
    (project / "deleted.txt").write_text("gone\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "deleted.txt"], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "second"], check=True)
    (project / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (project / "deleted.txt").unlink()
    (project / "untracked.txt").write_text("new\n", encoding="utf-8")
    workspace = GitWorkspace(timeout_ms=3000)
    snapshots, diffs = [], []
    workspace.status_changed.connect(snapshots.append)
    workspace.diff_changed.connect(diffs.append)
    workspace.set_root(project)
    workspace.refresh()
    assert _wait_until(qapp, lambda: bool(snapshots))
    changes = {change.path: change for change in snapshots[-1].changes}
    assert set(changes) == {"tracked.txt", "deleted.txt", "untracked.txt"}

    workspace.diff_for("untracked.txt", changes["untracked.txt"])
    assert _wait_until(qapp, lambda: bool(diffs))
    assert "+new" in diffs[-1].text
    workspace.diff_for("deleted.txt", changes["deleted.txt"])
    assert _wait_until(qapp, lambda: len(diffs) >= 2)
    assert "-gone" in diffs[-1].text
    workspace.diff_for("tracked.txt", changes["tracked.txt"])
    assert _wait_until(qapp, lambda: len(diffs) >= 3)
    assert "+changed" in diffs[-1].text
    workspace.diff_for("no-change.txt", None)
    assert _wait_until(qapp, lambda: len(diffs) >= 4)
    assert diffs[-1].text == ""
    workspace.close()


def test_git_diff_binary_and_large_output_are_protected(qapp, tmp_path: Path):
    project = tmp_path / "repo"
    project.mkdir()
    _git_project(project)
    (project / "binary.bin").write_bytes(b"\x00before")
    subprocess.run(["git", "-C", str(project), "add", "binary.bin"], check=True)
    subprocess.run(["git", "-C", str(project), "commit", "-qm", "binary"], check=True)
    (project / "binary.bin").write_bytes(b"\x00after")
    workspace = GitWorkspace(timeout_ms=3000, max_diff_bytes=128)
    snapshots, diffs = [], []
    workspace.status_changed.connect(snapshots.append)
    workspace.diff_changed.connect(diffs.append)
    workspace.set_root(project)
    workspace.refresh()
    assert _wait_until(qapp, lambda: bool(snapshots))
    binary = next(change for change in snapshots[-1].changes if change.path == "binary.bin")
    workspace.diff_for("binary.bin", binary)
    assert _wait_until(qapp, lambda: bool(diffs))
    assert diffs[-1].is_binary is True

    (project / "tracked.txt").write_text("x\n" * 1000, encoding="utf-8")
    workspace.diff_for("tracked.txt")
    assert _wait_until(qapp, lambda: len(diffs) >= 2)
    assert diffs[-1].too_large is True
    workspace.close()
