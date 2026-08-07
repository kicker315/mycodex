from mint_codex.core.controller import CodexController


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
    assert messages[0] == {"id": 10, "result": {"decision": "decline"}}
    assert messages[1] == {"id": 11, "result": {"decision": "decline"}}
    assert messages[2]["error"]["code"] == -32601
    assert all(message.get("result", {}).get("decision") not in {"accept", "acceptForSession"} for message in messages)


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
