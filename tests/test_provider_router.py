from mint_codex.core.provider_router import ProviderRouter
from mint_codex.core.backend import AppServerBackend
from mint_codex.models.session import Thread, ThreadSummary


class MemorySecretStore:
    storage_description = "test store"

    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, name):
        return self.values.get(name)

    def save(self, name, value):
        self.values[name] = value
        return True

    def delete(self, name):
        self.values.pop(name, None)
        return True


def test_thread_provider_is_the_only_backend_route(qapp, tmp_path):
    router = ProviderRouter(cwd=tmp_path)
    thread = Thread("same-id", provider="deepseek", model="deepseek-v4-flash")

    router.set_provider("openai")

    assert router.backend_for_thread(thread) is router.backends["deepseek"]


def test_resume_uses_provider_backend_even_when_picker_is_different(qapp, tmp_path):
    router = ProviderRouter(cwd=tmp_path)
    summary = ThreadSummary("deep-thread", "deepseek", "deepseek-v4-flash")
    backend = router.backends["deepseek"]
    backend._ready = True
    requests = []

    def request(method, params=None, callback=None):
        requests.append((method, params))
        if callback:
            callback({"thread": {"id": "deep-thread", "modelProvider": "deepseek", "model": "deepseek-v4-flash", "turns": []}}, None)
        return 1

    backend.request = request
    router._thread_summaries[(summary.provider, summary.id)] = summary
    router.set_provider("openai")
    router.resume_thread(summary)

    assert requests[0] == (
        "thread/resume",
        {"threadId": "deep-thread", "cwd": str(tmp_path.resolve()), "modelProvider": "deepseek"},
    )
    assert router.current_thread is not None
    assert router.current_thread.provider == "deepseek"


def test_resume_hydrates_persisted_turns_at_provider_seam(qapp, tmp_path):
    router = ProviderRouter(cwd=tmp_path)
    summary = ThreadSummary("deep-thread", "deepseek", "deepseek-v4-flash")
    backend = router.backends["deepseek"]
    backend._ready = True

    def request(method, params=None, callback=None):
        if callback:
            callback(
                {
                    "thread": {
                        "id": "deep-thread",
                        "modelProvider": "deepseek",
                        "model": "deepseek-v4-flash",
                        "turns": [
                            {
                                "id": "turn-1",
                                "status": "completed",
                                "items": [{"id": "item-1", "type": "agentMessage", "text": "old answer"}],
                            }
                        ],
                    }
                },
                None,
            )
        return 1

    backend.request = request
    router._thread_summaries[(summary.provider, summary.id)] = summary
    router.resume_thread(summary)

    assert router.current_thread is not None
    assert [turn.id for turn in router.current_thread.turns] == ["turn-1"]
    assert router.current_thread.turns[0].items[0].text == "old answer"


def test_new_thread_and_turn_use_current_project_cwd_and_reasoning_effort(qapp, tmp_path):
    router = ProviderRouter(cwd=tmp_path)
    backend = router.backends["openai"]
    backend._ready = True
    requests = []

    def request(method, params=None, callback=None):
        requests.append((method, params))
        if method == "thread/start" and callback:
            callback(
                {
                    "model": "gpt-5",
                    "reasoningEffort": "high",
                    "thread": {"id": "project-thread", "modelProvider": "openai"},
                },
                None,
            )
        elif method == "turn/start" and callback:
            callback({"turn": {"id": "turn-1", "status": "inProgress"}}, None)
        return 1

    backend.request = request
    router.set_provider("openai")
    router.set_model("request-model")
    router.set_reasoning_effort("high")
    router.send_text("inspect this project")

    assert requests[0] == (
        "thread/start",
        {"cwd": str(tmp_path.resolve()), "modelProvider": "openai", "model": "request-model"},
    )
    assert requests[1] == (
        "turn/start",
        {
            "threadId": "project-thread",
            "input": [{"type": "text", "text": "inspect this project"}],
            "cwd": str(tmp_path.resolve()),
            "model": "request-model",
            "effort": "high",
        },
    )
    assert router.current_thread is not None
    assert router.current_thread.model == "gpt-5"
    assert router.current_thread.reasoning_effort == "high"


def test_thread_management_operations_use_the_summary_provider_backend(qapp, tmp_path):
    router = ProviderRouter(cwd=tmp_path)
    summary = ThreadSummary("thread-1", "deepseek", "deepseek-v4-flash", "Old name", str(tmp_path))
    backend = router.backends["deepseek"]
    backend._ready = True
    requests = []

    def request(method, params=None, callback=None):
        requests.append((method, params))
        if callback:
            callback({}, None)
        return 1

    backend.request = request
    router.rename_thread(summary, "New name")
    router.archive_thread(summary)
    router.delete_thread(summary)

    assert requests == [
        ("thread/setName", {"threadId": "thread-1", "name": "New name"}),
        ("thread/archive", {"threadId": "thread-1"}),
        ("thread/delete", {"threadId": "thread-1"}),
    ]


def test_deepseek_key_is_saved_and_restarts_only_deepseek(qapp, tmp_path, monkeypatch):
    router = ProviderRouter(cwd=tmp_path, secret_store=MemorySecretStore())
    backend = router.backends["deepseek"]
    applied = []
    restarted = []
    statuses = []
    router.api_key_status_changed.connect(lambda provider, status: statuses.append((provider, status)))
    backend.set_secret_env_value = applied.append
    backend.restart = lambda: restarted.append(True)
    monkeypatch.setattr(type(backend), "is_running", property(lambda _backend: True))

    assert router.set_provider_api_key("deepseek", "fixture-value") is True

    assert applied == ["fixture-value"]
    assert restarted == [True]
    assert statuses[-1] == ("deepseek", "stored")
    assert "fixture-value" not in repr(router.providers["deepseek"])


def test_saved_deepseek_key_is_loaded_and_updated_through_provider_boundary(qapp, tmp_path, monkeypatch):
    store = MemorySecretStore({"deepseek": "fixture-value"})
    loaded = []
    monkeypatch.setattr(AppServerBackend, "set_secret_env_value", lambda backend, value: loaded.append(value))

    router = ProviderRouter(cwd=tmp_path, secret_store=store)

    assert router.provider_api_key_status("deepseek") == "stored"
    assert loaded == ["fixture-value"]
    assert router.set_provider_api_key("deepseek", "replacement-value") is True
    assert store.values["deepseek"] == "replacement-value"
    assert router.set_provider_api_key("deepseek", None) is True
    assert "deepseek" not in store.values


def test_key_change_is_rejected_during_active_deepseek_turn(qapp, tmp_path):
    router = ProviderRouter(cwd=tmp_path)
    router._thread = Thread("deep-thread", provider="deepseek", model="deepseek-v4-flash")
    router._busy = True
    backend = router.backends["deepseek"]
    applied = []
    backend.set_secret_env_value = applied.append
    errors = []
    router.error.connect(errors.append)

    assert router.set_provider_api_key("deepseek", "fixture-value") is False

    assert applied == []
    assert errors == ["Cannot change DeepSeek API key while a turn is running"]
