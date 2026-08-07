from __future__ import annotations

import json
import logging
from collections.abc import Callable

log = logging.getLogger(__name__)


class JsonLineFramer:
    """Incrementally decode the app-server's newline-delimited JSON transport."""

    def __init__(self, on_message: Callable[[dict], None], on_error: Callable[[str], None] | None = None):
        self._buffer = bytearray()
        self._on_message = on_message
        self._on_error = on_error or (lambda message: log.warning("%s", message))

    def feed(self, data: bytes) -> None:
        self._buffer.extend(data)
        while b"\n" in self._buffer:
            raw, _, remainder = self._buffer.partition(b"\n")
            self._buffer = bytearray(remainder)
            raw = raw.strip()
            if not raw:
                continue
            try:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("JSON-RPC message must be an object")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                self._on_error(f"Malformed app-server message: {exc}")
                continue
            try:
                self._on_message(value)
            except Exception as exc:
                log.exception("Failed to dispatch one app-server message")
                self._on_error(f"Invalid app-server message: {exc}")


def encode_message(message: dict) -> bytes:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
