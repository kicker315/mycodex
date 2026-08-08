from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class FileAccessError(RuntimeError):
    """A safe, user-facing failure while inspecting a Project file."""


@dataclass(frozen=True)
class FilePreview:
    relative_path: str
    text: str
    line_count: int
    size_bytes: int


def resolve_project_path(root: Path, candidate: str | Path, *, must_exist: bool = False) -> Path:
    """Resolve a path and enforce the active Project root, including symlinks."""

    project_root = Path(root).expanduser().resolve(strict=False)
    raw = Path(candidate).expanduser()
    target = raw if raw.is_absolute() else project_root / raw
    try:
        resolved = target.resolve(strict=False)
        resolved.relative_to(project_root)
    except (OSError, ValueError) as exc:
        raise FileAccessError("Path is outside the active Project") from exc
    if resolved == project_root:
        raise FileAccessError("A Project directory is not a file")
    if must_exist and not resolved.exists():
        raise FileAccessError("File does not exist")
    if resolved.exists() and resolved.is_dir():
        raise FileAccessError("A directory cannot be previewed as a file")
    return resolved


def read_project_file(
    root: Path,
    candidate: str | Path,
    *,
    max_bytes: int = 2 * 1024 * 1024,
) -> FilePreview:
    path = resolve_project_path(root, candidate, must_exist=True)
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise FileAccessError(f"File is too large to preview ({size} bytes)")
        data = path.read_bytes()
    except FileAccessError:
        raise
    except PermissionError as exc:
        raise FileAccessError("Permission denied while reading file") from exc
    except OSError as exc:
        raise FileAccessError(f"Could not read file: {exc}") from exc
    if b"\x00" in data:
        raise FileAccessError("Binary file cannot be previewed")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FileAccessError("File encoding is not valid UTF-8") from exc
    relative = path.relative_to(Path(root).expanduser().resolve(strict=False)).as_posix()
    return FilePreview(relative, text, text.count("\n") + (1 if text else 0), len(data))
