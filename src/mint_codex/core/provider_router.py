from __future__ import annotations

import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from mint_codex.models.session import Item, Thread, ThreadSummary, Turn
from mint_codex.models.timeline import ApprovalRequest

from .backend import AppServerBackend
from .providers import ProviderConfig, default_provider_configs
from .redaction import redact_sensitive
from .secrets import SecretStore

log = logging.getLogger(__name__)
BackendFactory = Callable[[ProviderConfig, Path, QObject], AppServerBackend]


class ProviderRouter(QObject):
    """Routes every operation to the App Server that owns its Provider Thread."""

    connection_changed = Signal(str)
    account_changed = Signal(str)
    provider_account_changed = Signal(str, str)
    models_changed = Signal(object, object)
    provider_changed = Signal(str)
    provider_models_changed = Signal(str, object, object)
    provider_status_changed = Signal(str, str)
    api_key_status_changed = Signal(str, str)
    thread_history_changed = Signal(object)
    thread_changed = Signal(object)
    timeline_event = Signal(str, object)
    busy_changed = Signal(bool)
    agent_status_changed = Signal(str)
    approval_requested = Signal(object)
    approval_resolved = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        cwd: Path | None = None,
        parent: QObject | None = None,
        providers: dict[str, ProviderConfig] | None = None,
        backend_factory: BackendFactory | None = None,
        secret_store: SecretStore | None = None,
    ):
        super().__init__(parent)
        self.cwd = (cwd or Path.cwd()).resolve()
        self.providers = providers or default_provider_configs()
        self.secret_store = secret_store or SecretStore()
        self.backends: dict[str, AppServerBackend] = {}
        self._selected_provider = next(iter(self.providers))
        self._selected_models: dict[str, str | None] = {provider_id: config.model for provider_id, config in self.providers.items()}
        self._selected_reasoning_efforts: dict[str, str | None] = {provider_id: None for provider_id in self.providers}
        self._models: dict[str, list[dict[str, Any]]] = {}
        self._accounts: dict[str, str] = {}
        self._connections: dict[str, str] = {}
        self._api_key_sources: dict[str, str] = {
            provider_id: self._environment_key_status(config)
            for provider_id, config in self.providers.items()
        }
        self._thread_summaries: dict[tuple[str, str], ThreadSummary] = {}
        self._thread: Thread | None = None
        self._workspace_generation = 0
        self._turns: dict[tuple[str, str], Turn] = {}
        self._items: dict[tuple[str, str], Item] = {}
        self._busy = False
        self._agent_status = "Ready"
        self._pending_approvals: dict[tuple[str, str], ApprovalRequest] = {}

        factory = backend_factory or (lambda config, path, owner: AppServerBackend(config, path, owner))
        for provider_id, config in self.providers.items():
            backend = factory(config, self.cwd, self)
            self.backends[provider_id] = backend
            backend.connection_changed.connect(self._on_backend_connection)
            backend.account_changed.connect(self._on_backend_account)
            backend.models_changed.connect(self._on_backend_models)
            backend.notification.connect(self._on_backend_notification)
            backend.server_request.connect(self._on_backend_server_request)
            backend.ready.connect(self._on_backend_ready)
            backend.error.connect(self._on_backend_error)

        for provider_id, config in self.providers.items():
            if not config.env_key:
                continue
            saved_key = self.secret_store.get(provider_id)
            if saved_key:
                self.backends[provider_id].set_secret_env_value(saved_key)
                self._api_key_sources[provider_id] = "stored"

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(self.providers)

    @property
    def selected_provider(self) -> str:
        return self._selected_provider

    @property
    def current_thread(self) -> Thread | None:
        return self._thread

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def agent_status(self) -> str:
        return self._agent_status

    def provider_status(self, provider_id: str) -> str:
        return self._connections.get(provider_id, "Disconnected")

    def provider_account_status(self, provider_id: str) -> str:
        return self._accounts.get(provider_id, "Account unavailable")

    @property
    def thread(self) -> Thread | None:
        """Compatibility alias used by the MVP-1 controller surface."""

        return self._thread

    @property
    def rpc(self):
        return self.backends[self._selected_provider].rpc

    @property
    def process(self):
        return self.backends[self._selected_provider].process

    def start(self) -> None:
        for backend in self.backends.values():
            backend.start()

    def stop(self) -> None:
        for backend in self.backends.values():
            backend.stop()

    def set_provider(self, provider_id: str) -> None:
        if provider_id not in self.backends:
            self._fail(f"Unknown provider: {provider_id}")
            return
        self._selected_provider = provider_id
        self.provider_changed.emit(provider_id)
        self.models_changed.emit(self._models.get(provider_id, []), self._selected_models.get(provider_id))
        self.account_changed.emit(self._accounts.get(provider_id, "Account unavailable"))
        self.connection_changed.emit(self._connections.get(provider_id, "Disconnected"))

    def set_model(self, model: str | None) -> None:
        self._selected_models[self._selected_provider] = model

    def set_reasoning_effort(self, effort: str | None) -> None:
        normalized = effort.strip() if isinstance(effort, str) else ""
        self._selected_reasoning_efforts[self._selected_provider] = normalized or None

    def set_workspace_path(self, path: Path) -> None:
        """Change the project cwd used by thread/start and thread/list."""

        self.cwd = path.expanduser().resolve()
        self._workspace_generation += 1
        self._thread_summaries.clear()
        self._replace_current(None)
        self._set_busy(False)
        self.refresh_history()

    def provider_api_key_status(self, provider_id: str) -> str:
        return self._api_key_sources.get(provider_id, "missing")

    def set_provider_api_key(self, provider_id: str, api_key: str | None) -> bool:
        config = self.providers.get(provider_id)
        backend = self.backends.get(provider_id)
        if config is None or backend is None:
            self._fail(f"Unknown provider: {provider_id}")
            return False
        if not config.env_key:
            self._fail(f"Provider {provider_id} does not accept an environment API key")
            return False
        if self._busy and self._thread is not None and self._thread.provider == provider_id:
            self.error.emit(f"Cannot change {config.display_name} API key while a turn is running")
            return False

        normalized = api_key.strip() if isinstance(api_key, str) else ""
        persisted = self.secret_store.delete(provider_id) if not normalized else self.secret_store.save(provider_id, normalized)
        if not persisted:
            self.error.emit(f"Could not persist {config.display_name} API key")
            return False
        backend.set_secret_env_value(normalized or None)
        source = "stored" if normalized else self._environment_key_status(config)
        self._api_key_sources[provider_id] = source
        self.api_key_status_changed.emit(provider_id, source)
        if backend.is_running:
            backend.restart()
        else:
            backend.start()
        return True

    def new_thread(self) -> None:
        self._replace_current(None)
        self._set_busy(False)
        self._set_agent_status("Ready")
        self.timeline_event.emit("new_thread", {"provider": self._selected_provider})

    def refresh_history(self) -> None:
        self._thread_summaries.clear()
        for provider_id, backend in self.backends.items():
            if backend.is_ready:
                self._list_backend_threads(provider_id, self._workspace_generation)

    def backend_for_thread(self, thread: Thread) -> AppServerBackend:
        """Resolve a Thread only through its recorded Provider, never the picker."""

        try:
            return self.backends[thread.provider]
        except KeyError as exc:
            raise ValueError(f"No App Server is registered for provider {thread.provider!r}") from exc

    def resume_thread(self, thread: ThreadSummary | tuple[str, str] | str, provider: str | None = None) -> None:
        summary = self._resolve_summary(thread, provider)
        if summary is None:
            return
        backend = self.backends.get(summary.provider)
        if backend is None:
            self._fail(f"No App Server is registered for provider {summary.provider}")
            return
        if summary.cwd and Path(summary.cwd).expanduser().resolve() != self.cwd:
            self._fail(summary.provider, "Thread belongs to a different Project directory")
            return
        if not backend.is_ready:
            self._fail(f"{self.providers[summary.provider].display_name} App Server is not connected")
            return
        params: dict[str, Any] = {"threadId": summary.id, "cwd": str(self.cwd)}
        params["modelProvider"] = summary.provider
        backend.request(
            "thread/resume",
            params,
            lambda result, error, expected=summary, generation=self._workspace_generation: self._thread_resumed(
                expected, result, error, generation
            ),
        )

    def rename_thread(self, thread: ThreadSummary | tuple[str, str] | str, name: str, provider: str | None = None) -> None:
        summary = self._resolve_summary(thread, provider)
        if summary is None:
            return
        normalized = name.strip()
        if not normalized:
            self._fail("Thread name cannot be empty")
            return
        self._thread_action(
            "thread/setName",
            summary,
            {"threadId": summary.id, "name": normalized},
            lambda result, error, expected=summary, title=normalized: self._thread_action_completed(
                "rename", expected, title, result, error
            ),
        )

    def archive_thread(self, thread: ThreadSummary | tuple[str, str] | str, provider: str | None = None) -> None:
        summary = self._resolve_summary(thread, provider)
        if summary is not None:
            self._thread_action(
                "thread/archive",
                summary,
                {"threadId": summary.id},
                lambda result, error, expected=summary: self._thread_action_completed(
                    "archive", expected, None, result, error
                ),
            )

    def delete_thread(self, thread: ThreadSummary | tuple[str, str] | str, provider: str | None = None) -> None:
        summary = self._resolve_summary(thread, provider)
        if summary is not None:
            self._thread_action(
                "thread/delete",
                summary,
                {"threadId": summary.id},
                lambda result, error, expected=summary: self._thread_action_completed(
                    "delete", expected, None, result, error
                ),
            )

    def _thread_action(self, method: str, summary: ThreadSummary, params: dict[str, Any], callback: Callback) -> None:
        backend = self.backends.get(summary.provider)
        if backend is None:
            self._fail(f"No App Server is registered for provider {summary.provider}")
            return
        if summary.cwd and Path(summary.cwd).expanduser().resolve() != self.cwd:
            self._fail(summary.provider, "Thread belongs to a different Project directory")
            return
        if not backend.is_ready:
            self._fail(f"{self.providers[summary.provider].display_name} App Server is not connected")
            return
        backend.request(method, params, callback)

    def _thread_action_completed(
        self,
        action: str,
        summary: ThreadSummary,
        title: str | None,
        result: object,
        error: dict | None,
    ) -> None:
        if error:
            self._fail(summary.provider, f"{action} failed: {error.get('message', error)}")
            return
        key = (summary.provider, summary.id)
        if action == "rename":
            updated = replace(summary, title=title)
            self._thread_summaries[key] = updated
            if self._thread and self._thread.id == summary.id and self._thread.provider == summary.provider:
                self._thread.title = title
                self.thread_changed.emit(self._thread)
        else:
            self._thread_summaries.pop(key, None)
            if self._thread and self._thread.id == summary.id and self._thread.provider == summary.provider:
                self._replace_current(None)
                self._set_busy(False)
        self._emit_history()

    def send_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self._thread is None:
            provider_id = self._selected_provider
            backend = self.backends[provider_id]
        else:
            provider_id = self._thread.provider
            backend = self.backend_for_thread(self._thread)
        if not backend.is_ready:
            self._fail(f"{self.providers[provider_id].display_name} App Server is not connected")
            return

        self._set_busy(True)
        self._set_agent_status("Working")
        self.timeline_event.emit("user", {"text": text})
        if self._thread is None:
            params: dict[str, Any] = {"cwd": str(self.cwd), "modelProvider": provider_id}
            model = self._selected_models.get(provider_id)
            if model:
                params["model"] = model
            backend.request(
                "thread/start",
                params,
                lambda result, error, pid=provider_id, message=text, generation=self._workspace_generation: self._thread_started(
                    pid, result, error, message, generation
                ),
            )
        else:
            self._start_turn(text)

    def _start_turn(self, text: str) -> None:
        thread = self._thread
        if thread is None:
            self._fail("No current thread")
            return
        backend = self.backend_for_thread(thread)
        params: dict[str, Any] = {
            "threadId": thread.id,
            "input": [{"type": "text", "text": text}],
            "cwd": str(self.cwd),
        }
        model = self._selected_models.get(thread.provider) or thread.model
        if model:
            params["model"] = model
        effort = self._selected_reasoning_efforts.get(thread.provider)
        if effort:
            params["effort"] = effort
        backend.request(
            "turn/start",
            params,
            lambda result, error, pid=thread.provider, tid=thread.id, generation=self._workspace_generation: self._turn_accepted(
                pid, tid, result, error, generation
            ),
        )

    def _list_backend_threads(self, provider_id: str, generation: int | None = None) -> None:
        generation = self._workspace_generation if generation is None else generation
        backend = self.backends[provider_id]
        params = {"cwd": str(self.cwd), "limit": 100, "modelProviders": [], "archived": False}
        backend.request(
            "thread/list",
            params,
            lambda result, error, pid=provider_id, expected_generation=generation: self._threads_read(
                pid, result, error, expected_generation
            ),
        )

    def _threads_read(
        self,
        provider_id: str,
        result: object,
        error: dict | None,
        generation: int | None = None,
    ) -> None:
        if generation is not None and generation != self._workspace_generation:
            log.debug(
                "Ignoring stale thread/list response provider=%s generation=%s current=%s",
                provider_id,
                generation,
                self._workspace_generation,
            )
            return
        if error:
            self._fail(provider_id, f"thread/list failed: {error.get('message', error)}")
            return
        if not isinstance(result, dict):
            self._fail(provider_id, "thread/list returned an invalid response")
            return
        rows = result.get("data", result.get("threads", []))
        if not isinstance(rows, list):
            self._fail(provider_id, "thread/list returned invalid thread data")
            return
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            reported_provider = row.get("modelProvider")
            if reported_provider and reported_provider != provider_id:
                log.error(
                    "Ignoring thread %s reported by %s backend as belonging to %s",
                    row["id"],
                    provider_id,
                    reported_provider,
                )
                continue
            summary = ThreadSummary(
                id=row["id"],
                provider=provider_id,
                model=row.get("model") if isinstance(row.get("model"), str) else self.providers[provider_id].model,
                title=self._thread_title(row),
                cwd=row.get("cwd") if isinstance(row.get("cwd"), str) else None,
                updated_at=self._updated_at(row),
            )
            self._thread_summaries[(provider_id, summary.id)] = summary
        self._emit_history()

    def _thread_started(
        self,
        provider_id: str,
        result: object,
        error: dict | None,
        text: str,
        generation: int | None = None,
    ) -> None:
        if generation is not None and generation != self._workspace_generation:
            log.debug("Ignoring stale thread/start response generation=%s current=%s", generation, self._workspace_generation)
            return
        if error or not isinstance(result, dict):
            self._fail(provider_id, f"thread/start failed: {(error or {}).get('message', 'invalid response')}")
            return
        data = result.get("thread", {})
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            self._fail(provider_id, "thread/start response has no thread id")
            return
        reported_provider = data.get("modelProvider")
        if reported_provider and reported_provider != provider_id:
            self._fail(provider_id, f"thread/start provider mismatch: expected {provider_id}, got {reported_provider}")
            return
        thread = Thread(
            data["id"],
            provider=provider_id,
            model=result.get("model") if isinstance(result.get("model"), str) else self._selected_models.get(provider_id),
            title=self._generated_thread_title(text) or self._thread_title(data),
            cwd=data.get("cwd") if isinstance(data.get("cwd"), str) else str(self.cwd),
            reasoning_effort=result.get("reasoningEffort") if isinstance(result.get("reasoningEffort"), str) else None,
        )
        self._replace_current(thread)
        self._thread_summaries[(provider_id, thread.id)] = ThreadSummary(
            id=thread.id,
            provider=provider_id,
            model=thread.model,
            title=thread.title,
            cwd=thread.cwd,
        )
        self._emit_history()
        self.timeline_event.emit(
            "thread",
            {"id": thread.id, "provider": thread.provider, "model": thread.model},
        )
        self._start_turn(text)

    def _thread_resumed(
        self,
        summary: ThreadSummary,
        result: object,
        error: dict | None,
        generation: int | None = None,
    ) -> None:
        if generation is not None and generation != self._workspace_generation:
            log.debug(
                "Ignoring stale thread/resume response thread=%s generation=%s current=%s",
                summary.id,
                generation,
                self._workspace_generation,
            )
            return
        if error or not isinstance(result, dict):
            self._fail(summary.provider, f"thread/resume failed: {(error or {}).get('message', 'invalid response')}")
            return
        data = result.get("thread", {})
        if not isinstance(data, dict) or data.get("id") != summary.id:
            self._fail(summary.provider, "thread/resume response has an unexpected thread id")
            return
        reported_provider = data.get("modelProvider")
        if reported_provider and reported_provider != summary.provider:
            self._fail(
                summary.provider,
                f"thread/resume provider mismatch: expected {summary.provider}, got {reported_provider}",
            )
            return
        thread = self._thread_from_data(summary.provider, data, summary, result)
        self._replace_current(thread)
        self._selected_provider = summary.provider
        self.provider_changed.emit(summary.provider)
        self.models_changed.emit(self._models.get(summary.provider, []), self._selected_models.get(summary.provider))
        self._set_busy(False)
        self.timeline_event.emit(
            "history_loaded",
            {"id": thread.id, "provider": thread.provider, "model": thread.model, "turns": len(thread.turns)},
        )

    def _thread_from_data(
        self,
        provider_id: str,
        data: dict[str, Any],
        summary: ThreadSummary,
        response: dict[str, Any] | None = None,
    ) -> Thread:
        response = response or {}
        thread = Thread(
            data["id"],
            provider=provider_id,
            model=response.get("model") if isinstance(response.get("model"), str) else summary.model,
            title=self._thread_title(data) or summary.title,
            cwd=response.get("cwd") if isinstance(response.get("cwd"), str) else data.get("cwd") if isinstance(data.get("cwd"), str) else summary.cwd,
            reasoning_effort=response.get("reasoningEffort") if isinstance(response.get("reasoningEffort"), str) else None,
        )
        turns = data.get("turns", [])
        if not isinstance(turns, list):
            turns = []
        for turn_data in turns:
            if not isinstance(turn_data, dict) or not isinstance(turn_data.get("id"), str):
                continue
            turn = Turn(turn_data["id"], status=turn_data.get("status", "completed"))
            self._turns[(provider_id, turn.id)] = turn
            thread.turns.append(turn)
            items = turn_data.get("items", [])
            if not isinstance(items, list):
                continue
            for item_data in items:
                if isinstance(item_data, dict) and isinstance(item_data.get("id"), str):
                    item = Item(item_data["id"], item_data.get("type", "unknown"), item_data)
                    item.completed = True
                    item.text = self._item_text(item_data)
                    turn.items.append(item)
                    self._items[(provider_id, item.id)] = item
        return thread

    def _turn_accepted(
        self,
        provider_id: str,
        thread_id: str,
        result: object,
        error: dict | None,
        generation: int | None = None,
    ) -> None:
        if generation is not None and generation != self._workspace_generation:
            log.debug(
                "Ignoring stale turn/start response provider=%s thread=%s generation=%s current=%s",
                provider_id,
                thread_id,
                generation,
                self._workspace_generation,
            )
            return
        if error:
            self._fail(provider_id, f"turn/start failed: {error.get('message', error)}")
            return
        if isinstance(result, dict):
            turn_data = result.get("turn", {})
            if isinstance(turn_data, dict) and isinstance(turn_data.get("id"), str):
                self._add_turn(provider_id, turn_data["id"], turn_data.get("status", "inProgress"), self._thread)

    def _add_turn(self, provider_id: str, turn_id: str, status: str = "inProgress", thread: Thread | None = None) -> Turn:
        key = (provider_id, turn_id)
        turn = self._turns.get(key)
        if turn is None:
            turn = Turn(turn_id, status=status)
            self._turns[key] = turn
            target = thread or self._thread
            if target is not None and turn not in target.turns:
                target.turns.append(turn)
        return turn

    def _on_backend_notification(self, provider_id: str, method: str, params: object) -> None:
        if not isinstance(params, dict):
            log.warning("Ignoring notification with non-object params: %s", method)
            return
        if not self._notification_belongs_to_current(provider_id, params):
            return
        if method == "turn/started":
            self._set_agent_status("Working")
            data = params.get("turn", {})
            if isinstance(data, dict) and isinstance(data.get("id"), str):
                self._add_turn(provider_id, data["id"], data.get("status", "inProgress"))
        elif method == "item/started":
            data = params.get("item", {})
            if not isinstance(data, dict) or not isinstance(data.get("id"), str):
                log.warning("Ignoring malformed notification method=%s", method)
                return
            item = Item(data["id"], data.get("type", "unknown"), data)
            self._items[(provider_id, item.id)] = item
            turn = self._turns.get((provider_id, params.get("turnId")))
            if turn:
                turn.items.append(item)
            self.timeline_event.emit("item_started", data)
        elif method == "item/agentMessage/delta":
            item_id = params.get("itemId")
            delta = params.get("delta")
            if not isinstance(item_id, str) or not isinstance(delta, str):
                log.warning("Ignoring malformed notification method=%s", method)
                return
            item = self._items.get((provider_id, item_id))
            if item is None:
                item = Item(item_id, "agentMessage", {"id": item_id, "type": "agentMessage", "text": ""})
                self._items[(provider_id, item_id)] = item
                turn = self._turns.get((provider_id, params.get("turnId")))
                if turn:
                    turn.items.append(item)
            item.text += delta
            self.timeline_event.emit("agent_delta", params)
        elif method == "item/commandExecution/outputDelta":
            item_id = params.get("itemId")
            delta = params.get("delta")
            if not isinstance(item_id, str) or not isinstance(delta, str):
                log.warning("Ignoring malformed notification method=%s", method)
                return
            item = self._items.get((provider_id, item_id))
            if item:
                item.text += delta
            self.timeline_event.emit("command_output_delta", params)
        elif method == "item/fileChange/outputDelta":
            if not isinstance(params.get("itemId"), str) or not isinstance(params.get("delta"), str):
                log.warning("Ignoring malformed notification method=%s", method)
                return
            self.timeline_event.emit("file_change_output_delta", params)
        elif method == "item/fileChange/patchUpdated":
            item_id = params.get("itemId")
            changes = params.get("changes")
            if not isinstance(item_id, str) or not isinstance(changes, list):
                log.warning("Ignoring malformed notification method=%s", method)
                return
            item = self._items.get((provider_id, item_id))
            if item:
                item.data = {**item.data, "changes": changes}
            self.timeline_event.emit("file_change_updated", params)
        elif method == "item/mcpToolCall/progress":
            if not isinstance(params.get("itemId"), str) or not isinstance(params.get("message"), str):
                log.warning("Ignoring malformed notification method=%s", method)
                return
            self.timeline_event.emit("tool_progress", params)
        elif method == "item/completed":
            data = params.get("item", {})
            if not isinstance(data, dict):
                log.warning("Ignoring malformed notification method=%s", method)
                return
            item_id = data.get("id")
            item = self._items.get((provider_id, item_id))
            if item:
                item.data = data
                item.completed = True
            self.timeline_event.emit("item_completed", data)
        elif method == "turn/completed":
            data = params.get("turn", {})
            if not isinstance(data, dict):
                log.warning("Ignoring malformed notification method=%s", method)
                return
            turn_id = data.get("id")
            turn = self._turns.get((provider_id, turn_id))
            if turn:
                turn.status = data.get("status", "completed")
            self.timeline_event.emit("turn_completed", data)
            status = data.get("status", "completed")
            if status not in {"completed", "interrupted"}:
                details = data.get("error") or data.get("lastError") or "Codex did not complete this turn"
                message = details.get("message", details) if isinstance(details, dict) else str(details)
                self.timeline_event.emit("turn_failed", {"status": status, "message": message})
                self.error.emit(f"Turn {status}: {message}")
                self._set_agent_status("Failed")
            else:
                self._set_agent_status("Completed")
            self._set_busy(False)
        elif method == "error":
            details = params.get("error", params)
            message = details.get("message", details) if isinstance(details, dict) else str(details)
            if params.get("willRetry"):
                message = f"{message} (Codex will retry)"
            message = self.backends[provider_id].redact(message)
            self.timeline_event.emit("error", {"message": message})
            self.error.emit(message)
            self._set_agent_status("Failed")

    def _notification_belongs_to_current(self, provider_id: str, params: dict[str, Any]) -> bool:
        if getattr(self, "_compatibility_mode", False):
            return True
        thread_id = params.get("threadId")
        if thread_id is None:
            return self._thread is not None and self._thread.provider == provider_id
        return self._thread is not None and self._thread.provider == provider_id and self._thread.id == thread_id

    def _on_backend_server_request(self, provider_id: str, request_id: object, method: str, params: object) -> None:
        backend = self.backends[provider_id]
        supported = {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "applyPatchApproval",
            "execCommandApproval",
        }
        if method not in supported:
            log.warning("Safely rejecting unknown server request provider=%s id=%s method=%s", provider_id, request_id, method)
            self.timeline_event.emit("unsupported_request", {"provider": provider_id, "method": method})
            backend.respond_error(request_id, -32601, f"Unsupported client handler: {method}")
            return

        request = self._approval_request(provider_id, request_id, method, params)
        if request is None:
            log.error("Rejecting malformed approval request provider=%s method=%s", provider_id, method)
            backend.respond_error(request_id, -32602, "Malformed approval request")
            return
        if self._thread is None or self._thread.provider != provider_id or self._thread.id != request.thread_id:
            log.error("Rejecting approval request for a non-current Thread provider=%s thread=%s", provider_id, request.thread_id)
            backend.respond_error(request_id, -4090, "Approval request is not for the current Thread")
            return
        key = (provider_id, str(request_id))
        if key in self._pending_approvals:
            backend.respond_error(request_id, -4091, "Duplicate approval request")
            return
        self._pending_approvals[key] = request
        self._set_agent_status("Waiting Approval")
        self.approval_requested.emit(request)

    def respond_to_approval(self, request_key: str, decision: str) -> bool:
        """Answer one pending App Server request; unknown decisions are rejected."""

        request = next(
            (value for (provider_id, key), value in self._pending_approvals.items() if value.request_key == request_key),
            None,
        )
        if request is None:
            self._fail(f"Unknown approval request: {request_key}")
            return False
        if decision not in request.allowed_decisions:
            self._fail(f"Unsupported approval decision: {decision}")
            return False
        backend = self.backends.get(request.provider_id)
        if backend is None or not backend.is_ready:
            self._fail(request.provider_id, "Approval App Server is not running")
            return False
        backend.respond(request.request_id, self._approval_response(request, decision))
        self._pending_approvals.pop((request.provider_id, str(request.request_id)), None)
        self.approval_resolved.emit({"request": request, "decision": decision})
        self._set_agent_status("Working")
        return True

    def _approval_request(
        self, provider_id: str, request_id: object, method: str, params: object
    ) -> ApprovalRequest | None:
        if not isinstance(request_id, (int, str)) or isinstance(request_id, bool) or not isinstance(params, dict):
            return None
        thread_id = params.get("threadId") or params.get("conversationId")
        turn_id = params.get("turnId") or "unknown-turn"
        item_id = params.get("itemId") or "unknown-item"
        if not isinstance(thread_id, str) or not isinstance(turn_id, str) or not isinstance(item_id, str):
            return None
        backend = self.backends[provider_id]
        command_value = params.get("command")
        if isinstance(command_value, str):
            command = command_value
        elif isinstance(command_value, list) and all(isinstance(part, str) for part in command_value):
            command = " ".join(command_value)
        else:
            command = None
        cwd = params.get("cwd") if isinstance(params.get("cwd"), str) else None
        reason = params.get("reason") if isinstance(params.get("reason"), str) else None
        return ApprovalRequest(
            provider_id=provider_id,
            request_id=request_id,
            method=method,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            command=backend.redact(command) if command else None,
            cwd=backend.redact(cwd) if cwd else None,
            reason=backend.redact(reason) if reason else None,
            raw_params=dict(params),
        )

    @staticmethod
    def _approval_response(request: ApprovalRequest, decision: str) -> dict[str, Any]:
        if not request.is_legacy:
            return {"decision": decision}
        legacy = {
            "accept": "approved",
            "acceptForSession": "approved_for_session",
            "decline": {"denied": {"rejection": "Declined by user"}},
            "cancel": "abort",
        }
        return {"decision": legacy[decision]}

    def _on_backend_ready(self, provider_id: str) -> None:
        if provider_id == self._selected_provider and not self._busy and not self._pending_approvals:
            self._set_agent_status("Ready")
        self._list_backend_threads(provider_id)

    def _on_backend_connection(self, provider_id: str, status: str) -> None:
        self._connections[provider_id] = status
        self.provider_status_changed.emit(provider_id, status)
        if provider_id == self._selected_provider:
            self.connection_changed.emit(status)
            if "failed" in status.lower() or status.lower().startswith("disconnected"):
                self._set_agent_status("Failed")

    def _on_backend_account(self, provider_id: str, status: str) -> None:
        self._accounts[provider_id] = status
        self.provider_account_changed.emit(provider_id, status)
        if provider_id == self._selected_provider:
            self.account_changed.emit(status)

    def _on_backend_models(self, provider_id: str, models: object, default: object) -> None:
        normalized: list[dict[str, Any]] = []
        for model in models if isinstance(models, list) else []:
            if not isinstance(model, dict):
                continue
            model_id = model.get("model") or model.get("id")
            if isinstance(model_id, str):
                normalized.append({**model, "model": model_id, "displayName": model.get("displayName", model_id)})
        configured_model = self.providers[provider_id].model
        if configured_model and self.providers[provider_id].is_custom:
            # Codex's model/list is a Codex catalog, not a provider's upstream
            # /models response. Keep the configured custom model visible and
            # avoid presenting OpenAI catalog entries in the DeepSeek picker.
            normalized = [model for model in normalized if model["model"] == configured_model]
            if not normalized:
                normalized = [{"model": configured_model, "displayName": configured_model, "isDefault": True}]
            default = configured_model
        elif not normalized and configured_model:
            normalized = [{"model": configured_model, "displayName": configured_model, "isDefault": True}]
            default = configured_model
        self._models[provider_id] = normalized
        if isinstance(default, str):
            self._selected_models[provider_id] = default
        self.provider_models_changed.emit(provider_id, normalized, self._selected_models.get(provider_id))
        if provider_id == self._selected_provider:
            self.models_changed.emit(normalized, self._selected_models.get(provider_id))

    def _on_backend_error(self, provider_id: str, message: str) -> None:
        if message:
            safe_message = redact_sensitive(message)
            log.warning("%s App Server: %s", provider_id, safe_message)
            self.error.emit(safe_message)

    def _replace_current(self, thread: Thread | None) -> None:
        self._thread = thread
        if thread is None:
            self._turns.clear()
            self._items.clear()
        else:
            self._selected_provider = thread.provider
        self.thread_changed.emit(thread)

    def _resolve_summary(self, thread: ThreadSummary | tuple[str, str] | str, provider: str | None) -> ThreadSummary | None:
        if isinstance(thread, ThreadSummary):
            return thread
        if isinstance(thread, tuple) and len(thread) == 2:
            return self._thread_summaries.get((thread[0], thread[1]))
        if provider:
            summary = self._thread_summaries.get((provider, thread))
        else:
            matches = [value for (pid, tid), value in self._thread_summaries.items() if tid == thread]
            summary = matches[0] if len(matches) == 1 else None
        if summary is None:
            self._fail(f"Thread {thread!r} is not known to a Provider")
        return summary

    def _emit_history(self) -> None:
        histories = sorted(
            self._thread_summaries.values(),
            key=lambda summary: summary.updated_at or "",
            reverse=True,
        )
        self.thread_history_changed.emit(histories)

    @staticmethod
    def _thread_title(data: dict[str, Any]) -> str | None:
        for key in ("name", "title", "preview"):
            if isinstance(data.get(key), str) and data[key].strip():
                return data[key].strip()
        return None

    @staticmethod
    def _updated_at(data: dict[str, Any]) -> str | None:
        for key in ("updatedAt", "updated_at", "recencyAt", "recency_at", "createdAt", "created_at"):
            if isinstance(data.get(key), (str, int, float)):
                return str(data[key])
        return None

    @staticmethod
    def _item_text(data: dict[str, Any]) -> str:
        if isinstance(data.get("text"), str):
            return data["text"]
        content = data.get("content")
        if isinstance(content, list):
            return "".join(item.get("text", "") for item in content if isinstance(item, dict))
        return ""

    @staticmethod
    def _environment_key_status(config: ProviderConfig) -> str:
        if config.env_key and os.environ.get(config.env_key, "").strip():
            return "environment"
        return "missing"

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.busy_changed.emit(busy)

    def _set_agent_status(self, status: str) -> None:
        if status == self._agent_status:
            return
        self._agent_status = status
        self.agent_status_changed.emit(status)

    def _fail(self, provider_or_message: str, message: str | None = None) -> None:
        if message is None:
            provider = self._selected_provider
            safe_message = provider_or_message
        else:
            provider = provider_or_message
            safe_message = message
        if provider in self.backends:
            safe_message = self.backends[provider].redact(safe_message)
        else:
            safe_message = redact_sensitive(safe_message)
        log.error("%s: %s", provider, safe_message)
        self.error.emit(safe_message)
        self._set_agent_status("Failed")
        self._set_busy(False)

    @staticmethod
    def _generated_thread_title(text: str, limit: int = 28) -> str | None:
        normalized = " ".join(text.split())
        if not normalized:
            return None
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 1].rstrip() + "…"
