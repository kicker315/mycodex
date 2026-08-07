from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Signal

from .framing import JsonLineFramer, encode_message

log = logging.getLogger(__name__)
Callback = Callable[[Any, dict | None], None]


class JsonRpcClient(QObject):
    notification = Signal(str, object)
    server_request = Signal(object, str, object)
    protocol_error = Signal(str)
    response_received = Signal(object, object)

    def __init__(self, writer: Callable[[bytes], None], parent: QObject | None = None):
        super().__init__(parent)
        self._writer = writer
        self._next_id = 1
        self._pending: dict[int | str, tuple[str, Callback | None]] = {}
        self._framer = JsonLineFramer(self._dispatch, self._framing_error)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def feed(self, data: bytes) -> None:
        self._framer.feed(data)

    def request(self, method: str, params: object | None = None, callback: Callback | None = None) -> int:
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, object] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._pending[request_id] = (method, callback)
        log.info("RPC request id=%s method=%s", request_id, method)
        try:
            self._writer(encode_message(message))
        except Exception as exc:
            self._pending.pop(request_id, None)
            failure = {"code": -32000, "message": f"Transport write failed: {exc}"}
            self._framing_error(failure["message"])
            if callback:
                try:
                    callback(None, failure)
                except Exception:
                    log.exception("RPC failure callback failed id=%s method=%s", request_id, method)
        return request_id

    def fail_pending(self, message: str) -> None:
        """Resolve outstanding requests when the transport disappears."""

        error = {"code": -32000, "message": message}
        pending, self._pending = self._pending, {}
        for request_id, (method, callback) in pending.items():
            log.warning("RPC request aborted id=%s method=%s", request_id, method)
            if callback:
                try:
                    callback(None, error)
                except Exception:
                    log.exception("RPC abort callback failed id=%s method=%s", request_id, method)

    def notify(self, method: str, params: object | None = None) -> None:
        message: dict[str, object] = {"method": method}
        if params is not None:
            message["params"] = params
        log.info("RPC notification method=%s", method)
        try:
            self._writer(encode_message(message))
        except Exception as exc:
            self._framing_error(f"Transport write failed: {exc}")

    def respond(self, request_id: int | str, result: object) -> None:
        try:
            self._writer(encode_message({"id": request_id, "result": result}))
        except Exception as exc:
            self._framing_error(f"Transport write failed: {exc}")

    def respond_error(self, request_id: int | str, code: int, message: str) -> None:
        try:
            self._writer(encode_message({"id": request_id, "error": {"code": code, "message": message}}))
        except Exception as exc:
            self._framing_error(f"Transport write failed: {exc}")

    def _framing_error(self, message: str) -> None:
        log.warning("%s", message)
        self.protocol_error.emit(message)

    def _dispatch(self, message: dict) -> None:
        if "method" in message:
            method = message.get("method")
            if not isinstance(method, str):
                self._framing_error("JSON-RPC method is not a string")
                return
            params = message.get("params", {})
            if "id" in message:
                request_id = message["id"]
                if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
                    self._framing_error("Server request has an invalid id")
                    return
                log.info("Server request id=%s method=%s", request_id, method)
                self.server_request.emit(request_id, method, params)
            else:
                log.info("Server notification method=%s", method)
                self.notification.emit(method, params)
            return
        if "id" in message and ("result" in message or "error" in message):
            request_id = message["id"]
            if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
                self._framing_error("Server response has an invalid id")
                return
            pending = self._pending.pop(request_id, None)
            if pending is None:
                self._framing_error(f"Response for unknown request id {request_id!r}")
                return
            method, callback = pending
            error = message.get("error")
            log.info("RPC response id=%s method=%s error=%s", request_id, method, bool(error))
            if callback:
                callback(message.get("result"), error)
            self.response_received.emit(request_id, message)
            return
        self._framing_error("Unrecognized JSON-RPC message")
