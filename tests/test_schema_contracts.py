import json
from pathlib import Path


SCHEMAS = Path(__file__).parents[1] / "schemas"


def load_schema(name):
    return json.loads((SCHEMAS / name).read_text())


def methods_in(name):
    found = set()

    def visit(value):
        if isinstance(value, dict):
            method = value.get("properties", {}).get("method", {})
            found.update(method.get("enum", []))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(load_schema(name))
    return found


def assert_top_level_shape(payload, schema_name):
    schema = load_schema(schema_name)
    assert set(schema.get("required", [])) <= set(payload)
    assert set(payload) <= set(schema.get("properties", {}))


def test_mvp_rpc_methods_come_from_generated_protocol_schema():
    requests = methods_in("ClientRequest.json")
    notifications = methods_in("ClientNotification.json")
    assert {
        "initialize",
        "account/read",
        "model/list",
        "thread/start",
        "thread/list",
        "thread/resume",
        "turn/start",
    } <= requests
    assert "initialized" in notifications


def test_initialize_and_initialized_payloads_match_schema():
    params = {
        "clientInfo": {"name": "mint-codex-desktop", "title": "Mint Codex Desktop", "version": "0.2.0"},
        "capabilities": {"experimentalApi": False},
    }
    assert_top_level_shape(params, "v1/InitializeParams.json")
    assert set(params["clientInfo"]) >= {"name", "version"}
    assert set(params["clientInfo"]) <= {"name", "title", "version"}
    assert methods_in("ClientNotification.json") == {"initialized"}


def test_account_model_thread_and_turn_params_match_schema():
    account = {"refreshToken": False}
    models = {}
    thread = {"cwd": "/workspace", "model": "dynamically-discovered-model"}
    turn = {
        "threadId": "thread-1",
        "input": [{"type": "text", "text": "hello"}],
        "model": "dynamically-discovered-model",
    }
    assert_top_level_shape(account, "v2/GetAccountParams.json")
    assert_top_level_shape(models, "v2/ModelListParams.json")
    assert_top_level_shape(thread, "v2/ThreadStartParams.json")
    assert_top_level_shape(turn, "v2/TurnStartParams.json")
    assert turn["input"][0]["type"] == "text"


def test_provider_aware_thread_params_match_schema():
    start = {"cwd": "/workspace", "model": "deepseek-v4-flash", "modelProvider": "deepseek"}
    resume = {"threadId": "thread-1", "modelProvider": "deepseek"}
    assert_top_level_shape(start, "v2/ThreadStartParams.json")
    assert_top_level_shape(resume, "v2/ThreadResumeParams.json")


def test_project_workspace_thread_operations_match_generated_schema():
    listing = {"cwd": "/workspace", "limit": 100, "modelProviders": [], "archived": False}
    rename = {"threadId": "thread-1", "name": "Refactor agent"}
    archive = {"threadId": "thread-1"}
    delete = {"threadId": "thread-1"}
    turn = {"threadId": "thread-1", "input": [{"type": "text", "text": "hello"}], "cwd": "/workspace", "effort": "high"}

    assert_top_level_shape(listing, "v2/ThreadListParams.json")
    assert_top_level_shape(rename, "v2/ThreadSetNameParams.json")
    assert_top_level_shape(archive, "v2/ThreadArchiveParams.json")
    assert_top_level_shape(delete, "v2/ThreadDeleteParams.json")
    assert_top_level_shape(turn, "v2/TurnStartParams.json")


def test_safe_approval_decisions_are_schema_values():
    command = load_schema("CommandExecutionRequestApprovalResponse.json")
    file_change = load_schema("FileChangeRequestApprovalResponse.json")

    def enum_values(schema, definition):
        values = set()
        for choice in schema["definitions"][definition]["oneOf"]:
            values.update(choice.get("enum", []))
        return values

    assert "decline" in enum_values(command, "CommandExecutionApprovalDecision")
    assert "decline" in enum_values(file_change, "FileChangeApprovalDecision")
