from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Item:
    id: str
    type: str
    data: dict[str, Any]
    text: str = ""
    completed: bool = False


@dataclass
class Turn:
    id: str
    items: list[Item] = field(default_factory=list)
    status: str = "inProgress"


@dataclass
class Thread:
    id: str
    provider: str = "openai"
    model: str | None = None
    title: str | None = None
    cwd: str | None = None
    turns: list[Turn] = field(default_factory=list)
    project_id: str | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class ThreadSummary:
    id: str
    provider: str
    model: str | None = None
    title: str | None = None
    cwd: str | None = None
    updated_at: str | None = None
    project_id: str | None = None
