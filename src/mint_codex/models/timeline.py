from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TimelineKind(StrEnum):
    USER_MESSAGE = "user_message"
    AGENT_MESSAGE = "agent_message"
    COMMAND_EXECUTION = "command_execution"
    FILE_CHANGE = "file_change"
    TOOL_CALL = "tool_call"
    APPROVAL_REQUEST = "approval_request"
    STATUS = "status"
    ERROR = "error"


@dataclass(frozen=True)
class ApprovalRequest:
    """A safe, UI-facing representation of an App Server server request."""

    provider_id: str
    request_id: int | str
    method: str
    thread_id: str
    turn_id: str
    item_id: str
    command: str | None = None
    cwd: str | None = None
    reason: str | None = None
    raw_params: dict[str, Any] = field(default_factory=dict, repr=False)
    task_id: str | None = None

    @property
    def request_key(self) -> str:
        prefix = f"task:{self.task_id}:" if self.task_id else ""
        return f"{prefix}{self.provider_id}:{self.request_id}"

    @property
    def is_legacy(self) -> bool:
        return self.method in {"applyPatchApproval", "execCommandApproval"}

    @property
    def allowed_decisions(self) -> tuple[str, ...]:
        if self.is_legacy:
            return ("accept", "acceptForSession", "decline", "cancel")
        return ("accept", "acceptForSession", "decline", "cancel")


@dataclass(frozen=True)
class TimelineItem:
    kind: TimelineKind
    item_id: str | None = None
    text: str = ""
    status: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_codex_item(cls, item: dict[str, Any]) -> TimelineItem:
        item_type = item.get("type")
        item_id = item.get("id") if isinstance(item.get("id"), str) else None
        status = item.get("status") if isinstance(item.get("status"), str) else None
        if item_type == "userMessage":
            return cls(TimelineKind.USER_MESSAGE, item_id, _user_content_text(item.get("content", [])), status)
        if item_type == "agentMessage":
            return cls(TimelineKind.AGENT_MESSAGE, item_id, str(item.get("text", "")), status)
        if item_type == "commandExecution":
            command = str(item.get("command", ""))
            output = item.get("aggregatedOutput")
            text = command
            if isinstance(output, str) and output:
                text = f"{command}\n{output}" if command else output
            return cls(
                TimelineKind.COMMAND_EXECUTION,
                item_id,
                text,
                status,
                {
                    "command": command,
                    "cwd": item.get("cwd"),
                    "aggregated_output": output or "",
                    "exit_code": item.get("exitCode"),
                },
            )
        if item_type == "fileChange":
            changes = item.get("changes")
            count = len(changes) if isinstance(changes, list) else 0
            paths = _change_paths(changes)
            return cls(
                TimelineKind.FILE_CHANGE,
                item_id,
                "\n".join(paths) if paths else f"File changes ({count})",
                status,
                {"changes": changes if isinstance(changes, list) else []},
            )
        if item_type in {"mcpToolCall", "dynamicToolCall", "collabToolCall"}:
            if item_type == "mcpToolCall":
                label = "/".join(str(item.get(key, "")) for key in ("server", "tool"))
            else:
                label = str(item.get("tool") or item.get("name") or item_type)
            return cls(TimelineKind.TOOL_CALL, item_id, label, status)
        return cls(TimelineKind.STATUS, item_id, str(item_type or "Codex item"), status)

    @classmethod
    def from_approval(cls, request: ApprovalRequest) -> TimelineItem:
        text = request.command or request.reason or "Codex requested approval"
        return cls(
            TimelineKind.APPROVAL_REQUEST,
            request.request_key,
            text,
            "waiting",
            {
                "request_key": request.request_key,
                "method": request.method,
                "provider": request.provider_id,
                "task_id": request.task_id,
                "thread_id": request.thread_id,
                "turn_id": request.turn_id,
                "item_id": request.item_id,
                "command": request.command or "",
                "cwd": request.cwd or "",
                "reason": request.reason or "",
                "allowed_decisions": request.allowed_decisions,
            },
        )

    @classmethod
    def from_event(cls, kind: str, data: object) -> TimelineItem | None:
        payload = data if isinstance(data, dict) else {}
        if kind == "user":
            return cls(TimelineKind.USER_MESSAGE, text=str(payload.get("text", "")))
        if kind in {"item_started", "item_completed"}:
            return cls.from_codex_item(payload)
        if kind == "agent_delta":
            item_id = payload.get("itemId") if isinstance(payload.get("itemId"), str) else None
            return cls(
                TimelineKind.AGENT_MESSAGE,
                item_id,
                str(payload.get("delta", "")),
                metadata={"delta": True},
            )
        if kind == "command_output_delta":
            item_id = payload.get("itemId") if isinstance(payload.get("itemId"), str) else None
            return cls(
                TimelineKind.COMMAND_EXECUTION,
                item_id,
                str(payload.get("delta", "")),
                metadata={"output_delta": True},
            )
        if kind == "file_change_updated":
            item_id = payload.get("itemId") if isinstance(payload.get("itemId"), str) else None
            changes = payload.get("changes") if isinstance(payload.get("changes"), list) else []
            return cls(
                TimelineKind.FILE_CHANGE,
                item_id,
                "\n".join(_change_paths(changes)) or "File changes",
                metadata={"changes": changes},
            )
        if kind == "file_change_output_delta":
            item_id = payload.get("itemId") if isinstance(payload.get("itemId"), str) else None
            return cls(
                TimelineKind.FILE_CHANGE,
                item_id,
                str(payload.get("delta", "")),
                metadata={"output_delta": True},
            )
        if kind == "tool_progress":
            item_id = payload.get("itemId") if isinstance(payload.get("itemId"), str) else None
            return cls(
                TimelineKind.TOOL_CALL,
                item_id,
                str(payload.get("message", "")),
                metadata={"progress": True},
            )
        if kind == "approval_requested" and isinstance(payload.get("request"), ApprovalRequest):
            return cls.from_approval(payload["request"])
        if kind == "turn_completed":
            status = payload.get("status") if isinstance(payload.get("status"), str) else "completed"
            return cls(TimelineKind.STATUS, status=status, text=f"Turn {status}")
        if kind == "turn_failed":
            return cls(TimelineKind.ERROR, text=str(payload.get("message", "Turn failed")))
        if kind == "error":
            return cls(TimelineKind.ERROR, text=str(payload.get("message", "Codex error")))
        if kind == "unsupported_request":
            return cls(TimelineKind.ERROR, text=f"Unsupported request: {payload.get('method', 'unknown')}")
        if kind == "status":
            return cls(TimelineKind.STATUS, text=str(payload.get("text", "")))
        return None


def _user_content_text(content: object) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
        elif isinstance(part, str):
            parts.append(part)
    return "".join(parts)


def _change_paths(changes: object) -> list[str]:
    if not isinstance(changes, list):
        return []
    paths: list[str] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = change.get("path")
        kind = change.get("kind")
        kind_name = kind.get("type") if isinstance(kind, dict) else kind
        if isinstance(path, str):
            paths.append(f"{kind_name}: {path}" if isinstance(kind_name, str) else path)
    return paths
