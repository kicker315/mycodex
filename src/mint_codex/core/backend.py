from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from .process import AppServerProcess
from .providers import ProviderConfig
from .rpc import Callback, JsonRpcClient

log = logging.getLogger(__name__)


class AppServerBackend(QObject):
    """A small provider-specific adapter around one Codex app-server process."""

    connection_changed = Signal(str, str)
    account_changed = Signal(str, str)
    models_changed = Signal(str, object, object)
    notification = Signal(str, str, object)
    server_request = Signal(str, object, str, object)
    ready = Signal(str)
    error = Signal(str, str)

    def __init__(self, provider: ProviderConfig, cwd: Path, parent: QObject | None = None):
        super().__init__(parent)
        self.provider = provider
        self.cwd = cwd
        self.process = AppServerProcess(
            codex_home=provider.codex_home,
            config_overrides=provider.config_overrides,
            env_key=provider.env_key,
            parent=self,
        )
        self.rpc = JsonRpcClient(self.process.write, self)
        self.process.stdout_received.connect(self.rpc.feed)
        self.process.started.connect(self._initialize)
        self.process.stopped.connect(self._process_stopped)
        self.process.failed.connect(self._process_failed)
        self.process.stderr_received.connect(lambda message: log.warning("%s App Server stderr: %s", self.provider.id, message))
        self.rpc.notification.connect(self._on_notification)
        self.rpc.server_request.connect(self._on_server_request)
        self.rpc.protocol_error.connect(lambda message: self.error.emit(self.provider.id, message))
        self._ready = False
        self._restarting = False

    @property
    def is_running(self) -> bool:
        return self.process.is_running

    @property
    def is_ready(self) -> bool:
        return self._ready

    def start(self) -> None:
        self.connection_changed.emit(self.provider.id, "Connecting…")
        self.process.start()

    def stop(self) -> None:
        self.process.stop()

    def set_secret_env_value(self, value: str | None) -> None:
        self.process.set_secret_env_value(value)

    def redact(self, text: str) -> str:
        return self.process.redact(text)

    def restart(self) -> None:
        self._ready = False
        if not self.is_running:
            self.start()
            return
        self._restarting = True
        self.process.stop()
        if not self.process.is_running:
            QTimer.singleShot(0, self._complete_restart)

    def request(self, method: str, params: object | None = None, callback: Callback | None = None) -> int:
        return self.rpc.request(method, params, callback)

    def respond(self, request_id: int | str, result: object) -> None:
        self.rpc.respond(request_id, result)

    def respond_error(self, request_id: int | str, code: int, message: str) -> None:
        self.rpc.respond_error(request_id, code, message)

    def _initialize(self) -> None:
        params = {
            "clientInfo": {"name": "mint-codex-desktop", "title": "Mint Codex Desktop", "version": "0.2.0"},
            "capabilities": {"experimentalApi": False},
        }
        self.request("initialize", params, self._initialized)

    def _initialized(self, result: object, error: dict | None) -> None:
        if error:
            self._fail(f"initialize failed: {error.get('message', error)}")
            return
        self.rpc.notify("initialized")
        self._ready = True
        self.connection_changed.emit(self.provider.id, "Connected to Codex App Server")
        self.ready.emit(self.provider.id)
        self.request("account/read", {"refreshToken": False}, self._account_read)
        self.request("model/list", {}, self._models_read)

    def _account_read(self, result: object, error: dict | None) -> None:
        if error or not isinstance(result, dict):
            self.account_changed.emit(self.provider.id, "Account unavailable")
            return
        account = result.get("account")
        if not account:
            status = "Not logged in" if result.get("requiresOpenaiAuth") else "No account required"
        elif isinstance(account, dict) and account.get("type") == "chatgpt":
            status = f"Logged in: {account.get('email') or 'ChatGPT'} ({account.get('planType', 'unknown')})"
        elif isinstance(account, dict):
            status = f"Logged in: {account.get('type', 'account')}"
        else:
            status = "Account available"
        self.account_changed.emit(self.provider.id, status)

    def _models_read(self, result: object, error: dict | None) -> None:
        if error or not isinstance(result, dict):
            message = f"model/list failed: {(error or {}).get('message', 'invalid response')}"
            self.error.emit(self.provider.id, self.redact(message))
            self.models_changed.emit(self.provider.id, [], self.provider.model)
            return
        models = result.get("data", [])
        if not isinstance(models, list):
            models = []
        default = next((m.get("model") for m in models if isinstance(m, dict) and m.get("isDefault")), None)
        if not default:
            default = self.provider.model
        self.models_changed.emit(self.provider.id, models, default)

    def _on_notification(self, method: str, params: object) -> None:
        self.notification.emit(self.provider.id, method, params)

    def _on_server_request(self, request_id: object, method: str, params: object) -> None:
        self.server_request.emit(self.provider.id, request_id, method, params)

    def _process_stopped(self, code: int, _status: int) -> None:
        self._ready = False
        self.rpc.fail_pending(f"{self.provider.display_name} App Server exited with code {code}")
        if self._restarting:
            QTimer.singleShot(0, self._complete_restart)
            return
        self.connection_changed.emit(self.provider.id, f"Disconnected (exit {code})")

    def _complete_restart(self) -> None:
        if not self._restarting:
            return
        self._restarting = False
        self.start()

    def _process_failed(self, message: str) -> None:
        self._ready = False
        self.rpc.fail_pending(f"{self.provider.display_name} App Server process failed: {message}")
        self.connection_changed.emit(self.provider.id, "App Server connection failed")
        self.error.emit(self.provider.id, self.redact(message))

    def _fail(self, message: str) -> None:
        safe_message = self.redact(message)
        log.error("%s: %s", self.provider.id, safe_message)
        self.error.emit(self.provider.id, safe_message)
