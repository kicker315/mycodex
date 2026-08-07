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
    turns: list[Turn] = field(default_factory=list)
