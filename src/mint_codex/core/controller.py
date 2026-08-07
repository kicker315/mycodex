from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from mint_codex.models.session import Item, Thread, Turn

from .process import AppServerProcess
from .redaction import redact_sensitive
from .rpc import JsonRpcClient

log = logging.getLogger(__name__)


class CodexController(QObject):
    connection_changed = Signal(str)
    account_changed = Signal(str)
    models_changed = Signal(object, object)
    timeline_event = Signal(str, object)
    busy_changed = Signal(bool)
    error = Signal(str)

    def __init__(self, cwd: Path | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.cwd = (cwd or Path.cwd()).resolve()
        self.process = AppServerProcess(self)
        self.rpc = JsonRpcClient(self.process.write, self)
        self.thread: Thread | None = None
        self._turns: dict[str, Turn] = {}
        self._items: dict[str, Item] = {}
        self._selected_model: str | None = None
        self.process.stdout_received.connect(self.rpc.feed)
        self.process.started.connect(self._initialize)
        self.process.stopped.connect(self._process_stopped)
        self.process.failed.connect(self._process_failed)
        self.rpc.notification.connect(self._on_notification)
        self.rpc.server_request.connect(self._on_server_request)
        self.rpc.protocol_error.connect(self.error.emit)

    def start(self) -> None:
        self.connection_changed.emit("Connecting…")
        self.process.start()

    def stop(self) -> None:
        self.process.stop()

    def set_model(self, model: str | None) -> None:
        self._selected_model = model

    def send_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if not self.process.is_running:
            self._fail("Codex App Server is not connected")
            return
        self.busy_changed.emit(True)
        self.timeline_event.emit("user", {"text": text})
        if self.thread is None:
            params: dict[str, Any] = {"cwd": str(self.cwd)}
            if self._selected_model:
                params["model"] = self._selected_model
            self.rpc.request("thread/start", params, lambda result, err: self._thread_started(result, err, text))
        else:
            self._start_turn(text)

    def _initialize(self) -> None:
        params = {
            "clientInfo": {"name": "mint-codex-desktop", "title": "Mint Codex Desktop", "version": "0.1.0"},
            "capabilities": {"experimentalApi": False},
        }
        self.rpc.request("initialize", params, self._initialized)

    def _initialized(self, result: object, error: dict | None) -> None:
        if error:
            self._fail(f"initialize failed: {error.get('message', error)}")
            return
        self.rpc.notify("initialized")
        self.connection_changed.emit("Connected to Codex App Server")
        self.rpc.request("account/read", {"refreshToken": False}, self._account_read)
        self.rpc.request("model/list", {}, self._models_read)

    def _account_read(self, result: object, error: dict | None) -> None:
        if error or not isinstance(result, dict):
            self.account_changed.emit("Account unavailable")
            code = error.get("code") if isinstance(error, dict) else None
            log.warning("account/read failed (code=%s)", code)
            return
        account = result.get("account")
        if not account:
            status = "Not logged in" if result.get("requiresOpenaiAuth") else "No account required"
        elif account.get("type") == "chatgpt":
            status = f"Logged in: {account.get('email') or 'ChatGPT'} ({account.get('planType', 'unknown')})"
        else:
            status = f"Logged in: {account.get('type', 'account')}"
        self.account_changed.emit(status)

    def _models_read(self, result: object, error: dict | None) -> None:
        if error or not isinstance(result, dict):
            self._fail(f"model/list failed: {(error or {}).get('message', 'invalid response')}")
            return
        models = result.get("data", [])
        default = next((m.get("model") for m in models if m.get("isDefault")), None)
        self.models_changed.emit(models, default)

    def _thread_started(self, result: object, error: dict | None, text: str) -> None:
        if error or not isinstance(result, dict):
            self._fail(f"thread/start failed: {(error or {}).get('message', 'invalid response')}")
            return
        data = result.get("thread", {})
        thread_id = data.get("id")
        if not thread_id:
            self._fail("thread/start response has no thread id")
            return
        self.thread = Thread(thread_id)
        self.timeline_event.emit("thread", {"id": thread_id})
        self._start_turn(text)

    def _start_turn(self, text: str) -> None:
        assert self.thread is not None
        params: dict[str, Any] = {"threadId": self.thread.id, "input": [{"type": "text", "text": text}]}
        if self._selected_model:
            params["model"] = self._selected_model
        self.rpc.request("turn/start", params, self._turn_accepted)

    def _turn_accepted(self, result: object, error: dict | None) -> None:
        if error:
            self._fail(f"turn/start failed: {error.get('message', error)}")
            return
        if isinstance(result, dict):
            turn_data = result.get("turn", {})
            turn_id = turn_data.get("id")
            if turn_id and turn_id not in self._turns:
                self._add_turn(turn_id, turn_data.get("status", "inProgress"))

    def _add_turn(self, turn_id: str, status: str = "inProgress") -> Turn:
        turn = Turn(turn_id, status=status)
        self._turns[turn_id] = turn
        if self.thread:
            self.thread.turns.append(turn)
        return turn

    def _on_notification(self, method: str, params: object) -> None:
        if not isinstance(params, dict):
            log.warning("Ignoring notification with non-object params: %s", method)
            return
        if method == "turn/started":
            data = params.get("turn", {})
            if not isinstance(data, dict):
                log.warning("Ignoring malformed notification method=%s", method)
                return
            turn_id = data.get("id")
            if turn_id and turn_id not in self._turns:
                self._add_turn(turn_id, data.get("status", "inProgress"))
        elif method == "item/started":
            data = params.get("item", {})
            if not isinstance(data, dict):
                log.warning("Ignoring malformed notification method=%s", method)
                return
            item_id = data.get("id")
            if item_id:
                item = Item(item_id, data.get("type", "unknown"), data)
                self._items[item_id] = item
                turn = self._turns.get(params.get("turnId"))
                if turn:
                    turn.items.append(item)
                self.timeline_event.emit("item_started", data)
        elif method == "item/agentMessage/delta":
            item_id = params.get("itemId")
            delta = params.get("delta")
            if not isinstance(item_id, str) or not isinstance(delta, str):
                log.warning("Ignoring malformed notification method=%s", method)
                return
            item = self._items.get(item_id)
            if item:
                item.text += delta
            self.timeline_event.emit("agent_delta", params)
        elif method == "item/completed":
            data = params.get("item", {})
            if not isinstance(data, dict):
                log.warning("Ignoring malformed notification method=%s", method)
                return
            item = self._items.get(data.get("id"))
            if item:
                item.data = data
                item.completed = True
            self.timeline_event.emit("item_completed", data)
        elif method == "turn/completed":
            data = params.get("turn", {})
            if not isinstance(data, dict):
                log.warning("Ignoring malformed notification method=%s", method)
                return
            turn = self._turns.get(data.get("id"))
            if turn:
                turn.status = data.get("status", "completed")
            self.timeline_event.emit("turn_completed", data)
            status = data.get("status", "completed")
            if status not in {"completed", "interrupted"}:
                details = data.get("error") or data.get("lastError") or "Codex did not complete this turn"
                message = details.get("message", details) if isinstance(details, dict) else str(details)
                self.timeline_event.emit("turn_failed", {"status": status, "message": message})
                self.error.emit(f"Turn {status}: {message}")
            self.busy_changed.emit(False)
        elif method == "error":
            details = params.get("error", params)
            message = details.get("message", details) if isinstance(details, dict) else str(details)
            if params.get("willRetry"):
                message = f"{message} (Codex will retry)"
            self.timeline_event.emit("error", {"message": message})
            self.error.emit(message)
        else:
            log.debug("Unhandled notification method=%s", method)

    def _process_stopped(self, code: int, _status: int) -> None:
        self.rpc.fail_pending(f"Codex App Server exited with code {code}")
        self.busy_changed.emit(False)
        self.connection_changed.emit(f"Disconnected (exit {code})")

    def _process_failed(self, message: str) -> None:
        self.rpc.fail_pending(f"Codex App Server process failed: {message}")
        self.busy_changed.emit(False)
        self.connection_changed.emit("Codex App Server connection failed")
        self.error.emit(message)

    def _on_server_request(self, request_id: object, method: str, params: object) -> None:
        log.warning("Safely rejecting unsupported server request id=%s method=%s", request_id, method)
        self.timeline_event.emit("unsupported_request", {"method": method})
        if method == "item/commandExecution/requestApproval":
            self.rpc.respond(request_id, {"decision": "decline"})
        elif method == "item/fileChange/requestApproval":
            self.rpc.respond(request_id, {"decision": "decline"})
        elif method in {"applyPatchApproval", "execCommandApproval"}:
            self.rpc.respond(request_id, {"decision": {"denied": {"rejection": "Approval UI is not implemented"}}})
        else:
            self.rpc.respond_error(request_id, -32601, f"Unsupported client handler: {method}")

    def _fail(self, message: str) -> None:
        log.error("%s", redact_sensitive(message))
        self.error.emit(message)
        self.busy_changed.emit(False)
