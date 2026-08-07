import json

from mint_codex.core.framing import JsonLineFramer, encode_message


def test_partial_and_multiple_json_lines():
    messages, errors = [], []
    framer = JsonLineFramer(messages.append, errors.append)
    framer.feed(b'{"id":1,"res')
    framer.feed(b'ult":{}}\n{"method":"tick","params":{}}\n')
    assert messages == [{"id": 1, "result": {}}, {"method": "tick", "params": {}}]
    assert errors == []


def test_malformed_line_does_not_stop_following_message():
    messages, errors = [], []
    framer = JsonLineFramer(messages.append, errors.append)
    framer.feed(b'not json\n{"method":"ok"}\n')
    assert messages == [{"method": "ok"}]
    assert len(errors) == 1


def test_encode_is_compact_utf8_jsonl():
    encoded = encode_message({"method": "say", "params": {"text": "你好"}})
    assert encoded.endswith(b"\n")
    assert json.loads(encoded) == {"method": "say", "params": {"text": "你好"}}
