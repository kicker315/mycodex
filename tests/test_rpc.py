import json

from mint_codex.core.rpc import JsonRpcClient


def decode(writes):
    return [json.loads(value) for value in writes]


def test_request_id_correlation_and_response_dispatch(qapp):
    writes, callbacks, responses = [], [], []
    client = JsonRpcClient(writes.append)
    client.response_received.connect(lambda request_id, message: responses.append((request_id, message)))
    first = client.request("first", {}, lambda result, error: callbacks.append(("first", result, error)))
    second = client.request("second", {}, lambda result, error: callbacks.append(("second", result, error)))
    client.feed(f'{{"id":{second},"result":{{"ok":2}}}}\n'.encode())
    client.feed(f'{{"id":{first},"error":{{"code":-1,"message":"bad"}}}}\n'.encode())
    assert callbacks[0] == ("second", {"ok": 2}, None)
    assert callbacks[1][0] == "first"
    assert callbacks[1][2]["code"] == -1
    assert [x[0] for x in responses] == [second, first]
    assert client.pending_count == 0


def test_notification_routing_including_unknown(qapp):
    writes, seen = [], []
    client = JsonRpcClient(writes.append)
    client.notification.connect(lambda method, params: seen.append((method, params)))
    client.feed(b'{"method":"future/event","params":{"x":1}}\n')
    assert seen == [("future/event", {"x": 1})]


def test_server_request_routing_and_response(qapp):
    writes, seen = [], []
    client = JsonRpcClient(writes.append)
    client.server_request.connect(lambda request_id, method, params: seen.append((request_id, method, params)))
    client.feed(b'{"id":"server-1","method":"item/fileChange/requestApproval","params":{"reason":"x"}}\n')
    assert seen == [("server-1", "item/fileChange/requestApproval", {"reason": "x"})]
    client.respond("server-1", {"decision": "decline"})
    assert decode(writes) == [{"id": "server-1", "result": {"decision": "decline"}}]


def test_malformed_and_unknown_response_are_reported(qapp):
    writes, errors = [], []
    client = JsonRpcClient(writes.append)
    client.protocol_error.connect(errors.append)
    client.feed(b'bad\n{"id":999,"result":{}}\n')
    assert len(errors) == 2


def test_invalid_response_id_does_not_block_following_message(qapp):
    writes, errors, notifications = [], [], []
    client = JsonRpcClient(writes.append)
    client.protocol_error.connect(errors.append)
    client.notification.connect(lambda method, params: notifications.append((method, params)))
    client.feed(b'{"id":[],"result":{}}\n{"method":"still/alive","params":{}}\n')
    assert errors == ["Server response has an invalid id"]
    assert notifications == [("still/alive", {})]


def test_request_and_notification_wire_shape(qapp):
    writes = []
    client = JsonRpcClient(writes.append)
    request_id = client.request("model/list", {})
    client.notify("initialized")
    assert decode(writes) == [
        {"id": request_id, "method": "model/list", "params": {}},
        {"method": "initialized"},
    ]


def test_failed_write_does_not_leak_pending_or_raise(qapp):
    callbacks, errors = [], []

    def broken_writer(_data):
        raise RuntimeError("closed")

    client = JsonRpcClient(broken_writer)
    client.protocol_error.connect(errors.append)
    client.request("thread/start", {}, lambda result, error: callbacks.append((result, error)))
    assert client.pending_count == 0
    assert callbacks[0][0] is None
    assert callbacks[0][1]["code"] == -32000
    assert errors == ["Transport write failed: closed"]


def test_transport_exit_fails_all_pending_requests(qapp):
    callbacks = []
    client = JsonRpcClient(lambda _data: None)
    client.request("one", {}, lambda result, error: callbacks.append((result, error)))
    client.request("two", {}, lambda result, error: callbacks.append((result, error)))
    client.fail_pending("server exited")
    assert client.pending_count == 0
    assert [error["message"] for _, error in callbacks] == ["server exited", "server exited"]
