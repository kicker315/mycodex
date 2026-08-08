from mint_codex.models.timeline import ApprovalRequest, TimelineItem, TimelineKind
from mint_codex.ui.timeline import TimelineWidget
from PySide6.QtWidgets import QPushButton, QToolButton


def test_codex_items_are_mapped_to_domain_timeline_items():
    user = TimelineItem.from_codex_item(
        {"id": "user-1", "type": "userMessage", "content": [{"type": "text", "text": "Inspect the project"}]}
    )
    command = TimelineItem.from_codex_item(
        {"id": "cmd-1", "type": "commandExecution", "command": "pytest -q", "status": "completed"}
    )
    tool = TimelineItem.from_codex_item(
        {"id": "tool-1", "type": "mcpToolCall", "server": "docs", "tool": "search", "status": "completed"}
    )

    assert user == TimelineItem(TimelineKind.USER_MESSAGE, "user-1", "Inspect the project")
    assert command.kind == TimelineKind.COMMAND_EXECUTION
    assert "pytest -q" in command.text
    assert tool.kind == TimelineKind.TOOL_CALL
    assert "docs/search" in tool.text


def test_protocol_debug_events_do_not_become_chat_timeline_items():
    assert TimelineItem.from_event("thread", {"id": "thread-1"}) is None
    assert TimelineItem.from_event("history_loaded", {"turns": 2}) is None


def test_streaming_events_map_to_stable_domain_item_ids():
    command = TimelineItem.from_event(
        "command_output_delta", {"itemId": "cmd-1", "delta": "ok\n"}
    )
    file_change = TimelineItem.from_event(
        "file_change_updated",
        {"itemId": "file-1", "changes": [{"path": "tests/test_app.py", "kind": {"type": "update"}}]},
    )
    tool = TimelineItem.from_event(
        "tool_progress", {"itemId": "tool-1", "message": "Searching documentation"}
    )

    assert command.kind == TimelineKind.COMMAND_EXECUTION
    assert command.item_id == "cmd-1"
    assert file_change.kind == TimelineKind.FILE_CHANGE
    assert "tests/test_app.py" in file_change.text
    assert tool.kind == TimelineKind.TOOL_CALL


def test_approval_request_is_a_domain_timeline_item():
    request = ApprovalRequest("openai", 7, "item/commandExecution/requestApproval", "t", "turn", "item", "pytest -q")
    item = TimelineItem.from_approval(request)
    assert item.kind == TimelineKind.APPROVAL_REQUEST
    assert item.item_id == "openai:7"
    assert "acceptForSession" in item.metadata["allowed_decisions"]


def test_timeline_aggregates_agent_deltas_and_exposes_approval_buttons(qapp):
    timeline = TimelineWidget()
    timeline.apply_event("agent_delta", {"itemId": "agent-1", "delta": "hel"})
    timeline.apply_event("agent_delta", {"itemId": "agent-1", "delta": "lo"})
    assert timeline._items["agent-1"].text == "hello"

    request = ApprovalRequest("openai", 8, "item/commandExecution/requestApproval", "t", "turn", "item", "pytest")
    decisions = []
    timeline.approval_decision.connect(lambda key, decision: decisions.append((key, decision)))
    timeline.add_approval_request(request)
    allow = timeline.findChild(QPushButton, "approval_accept")
    assert allow is not None
    allow.click()
    assert decisions == [(request.request_key, "accept")]


def test_file_change_card_can_locate_a_changed_file(qapp):
    timeline = TimelineWidget()
    selected = []
    timeline.file_change_requested.connect(selected.append)
    timeline.apply_event(
        "item_started",
        {
            "id": "file-1",
            "type": "fileChange",
            "status": "inProgress",
            "changes": [{"path": "tests/test_app.py", "kind": {"type": "update"}}],
        },
    )
    button = next(button for button in timeline.findChildren(QToolButton) if button.text() == "tests/test_app.py")
    button.click()
    assert selected == ["tests/test_app.py"]
