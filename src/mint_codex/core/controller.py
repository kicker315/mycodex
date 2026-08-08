from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject

from .provider_router import ProviderRouter


class CodexController(ProviderRouter):
    """MVP-1 compatibility facade for the MVP-2 ProviderRouter."""

    def __init__(self, cwd: Path | None = None, parent: QObject | None = None):
        super().__init__(cwd=cwd, parent=parent)
        self._compatibility_mode = False

    def _on_notification(self, method: str, params: object) -> None:
        """Keep the old test/integration seam while real events are provider-tagged."""

        self._compatibility_mode = True
        try:
            super()._on_backend_notification(self.selected_provider, method, params)
        finally:
            self._compatibility_mode = False

    def _on_server_request(self, request_id: object, method: str, params: object) -> None:
        super()._on_backend_server_request(self.selected_provider, request_id, method, params)

    def _process_failed(self, message: str) -> None:
        self.connection_changed.emit("Codex App Server connection failed")
        self._set_agent_status("Failed")
        self.error.emit(message)
        self._set_busy(False)

    def _process_stopped(self, code: int, status: int) -> None:
        self._on_backend_connection(self.selected_provider, f"Disconnected (exit {code})")
        self._set_busy(False)
