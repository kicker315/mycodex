from mint_codex.core.controller import CodexController
from mint_codex.models.session import Thread


def test_failed_turn_is_visible_and_clears_busy(qapp):
    controller = CodexController()
    events, errors, busy = [], [], []
    controller.timeline_event.connect(lambda kind, data: events.append((kind, data)))
    controller.error.connect(errors.append)
    controller.busy_changed.connect(busy.append)
    controller._on_notification("turn/completed", {
        "threadId": "thread-1",
        "turn": {"id": "turn-1", "status": "failed", "error": {"message": "route-aware request timed out"}},
    })
    assert ("turn_failed", {"status": "failed", "message": "route-aware request timed out"}) in events
    assert errors == ["Turn failed: route-aware request timed out"]
    assert busy == [False]


def test_error_notification_is_routed_to_timeline(qapp):
    controller = CodexController()
    events, errors = [], []
    controller.timeline_event.connect(lambda kind, data: events.append((kind, data)))
    controller.error.connect(errors.append)
    controller._on_notification("error", {
        "error": {"message": "upstream unavailable"},
        "threadId": "thread-1", "turnId": "turn-1", "willRetry": False,
    })
    assert events == [("error", {"message": "upstream unavailable"})]
    assert errors == ["upstream unavailable"]


def test_command_file_and_tool_streams_are_routed_as_timeline_events(qapp):
    controller = CodexController()
    controller._replace_current(Thread("thread-1", provider="openai"))
    events = []
    controller.timeline_event.connect(lambda kind, data: events.append((kind, data)))
    controller._on_notification(
        "item/started",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {"id": "cmd-1", "type": "commandExecution", "command": "pytest -q", "cwd": "/workspace"},
        },
    )
    controller._on_notification(
        "item/commandExecution/outputDelta",
        {"threadId": "thread-1", "turnId": "turn-1", "itemId": "cmd-1", "delta": "1 passed"},
    )
    controller._on_notification(
        "item/fileChange/patchUpdated",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "file-1",
            "changes": [{"path": "tests/test_app.py", "kind": {"type": "update"}}],
        },
    )
    controller._on_notification(
        "item/mcpToolCall/progress",
        {"threadId": "thread-1", "turnId": "turn-1", "itemId": "tool-1", "message": "done"},
    )

    assert [kind for kind, _data in events] == [
        "item_started",
        "command_output_delta",
        "file_change_updated",
        "tool_progress",
    ]
    assert controller._items[("openai", "cmd-1")].text == "1 passed"


def test_semantically_malformed_notifications_are_fail_safe(qapp):
    controller = CodexController()
    for method, params in [
        ("turn/started", {"turn": []}),
        ("item/started", {"item": "bad"}),
        ("item/agentMessage/delta", {"itemId": "i", "delta": 42}),
        ("item/completed", {"item": None}),
        ("turn/completed", {"turn": []}),
    ]:
        controller._on_notification(method, params)


def test_approval_requests_are_never_automatically_approved(qapp):
    controller = CodexController()
    writes = []
    controller.rpc._writer = writes.append
    controller._on_server_request(10, "item/commandExecution/requestApproval", {})
    controller._on_server_request(11, "item/fileChange/requestApproval", {})
    controller._on_server_request(12, "item/permissions/requestApproval", {})
    import json
    messages = [json.loads(wire) for wire in writes]
    assert messages[0]["error"]["code"] == -32602
    assert messages[1]["error"]["code"] == -32602
    assert messages[2]["error"]["code"] == -32601
    assert all("result" not in message for message in messages)


def test_approval_request_is_presented_and_answered_over_bidirectional_rpc(qapp):
    controller = CodexController()
    backend = controller.backends["openai"]
    backend._ready = True
    controller._replace_current(Thread("thread-1", provider="openai"))
    writes = []
    backend.rpc._writer = writes.append
    requests = []
    controller.approval_requested.connect(requests.append)

    controller._on_server_request(
        10,
        "item/commandExecution/requestApproval",
        {
            "approvalId": None,
            "command": "pytest -q",
            "cwd": "/workspace",
            "itemId": "item-1",
            "reason": "Run tests",
            "threadId": "thread-1",
            "turnId": "turn-1",
            "startedAtMs": 1,
        },
    )

    assert len(requests) == 1
    assert controller.agent_status == "Waiting Approval"
    assert writes == []
    assert controller.respond_to_approval(requests[0].request_key, "acceptForSession")
    import json

    assert json.loads(writes[0]) == {"id": 10, "result": {"decision": "acceptForSession"}}
    assert controller.agent_status == "Working"


def test_unknown_approval_decision_is_fail_closed(qapp):
    controller = CodexController()
    backend = controller.backends["openai"]
    backend._ready = True
    controller._replace_current(Thread("thread-1", provider="openai"))
    writes = []
    backend.rpc._writer = writes.append
    requests = []
    controller.approval_requested.connect(requests.append)
    controller._on_server_request(
        11,
        "item/fileChange/requestApproval",
        {"itemId": "item-1", "threadId": "thread-1", "turnId": "turn-1", "startedAtMs": 1},
    )

    assert not controller.respond_to_approval(requests[0].request_key, "autoApprove")
    assert writes == []


def test_process_failure_clears_busy_and_connection_state(qapp):
    controller = CodexController()
    busy, connections, errors = [], [], []
    controller.busy_changed.connect(busy.append)
    controller.connection_changed.connect(connections.append)
    controller.error.connect(errors.append)
    controller._process_failed("failed to start")
    assert busy == [False]
    assert connections == ["Codex App Server connection failed"]
    assert errors == ["failed to start"]
