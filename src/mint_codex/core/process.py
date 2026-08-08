from __future__ import annotations

import logging
import os
from pathlib import Path
from collections.abc import Sequence

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from .redaction import redact_sensitive

log = logging.getLogger(__name__)


class AppServerProcess(QObject):
    stdout_received = Signal(bytes)
    stderr_received = Signal(str)
    started = Signal()
    stopped = Signal(int, int)
    failed = Signal(str)

    def __init__(
        self,
        codex_home: Path | None = None,
        config_overrides: Sequence[str] = (),
        env_key: str | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.codex_home = codex_home.expanduser().resolve() if codex_home else None
        self.config_overrides = tuple(config_overrides)
        self.env_key = env_key
        self._secret_env_value: str | None = None
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.started.connect(self._started)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)

    def start(self) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning:
            log.info("Starting Codex app-server")
            if self.codex_home:
                # Codex refuses a CODEX_HOME path that does not exist. This
                # only creates the provider-owned directory; it never writes
                # the user's existing ~/.codex/config.toml.
                self.codex_home.mkdir(parents=True, exist_ok=True)
                environment = QProcessEnvironment.systemEnvironment()
                environment.insert("CODEX_HOME", str(self.codex_home))
                if self.env_key and self._secret_env_value is not None:
                    environment.insert(self.env_key, self._secret_env_value)
                self.process.setProcessEnvironment(environment)
            arguments = ["app-server", "--stdio"]
            for override in self.config_overrides:
                arguments.extend(("-c", override))
            self.process.start("codex", arguments)

    @property
    def is_running(self) -> bool:
        return self.process.state() == QProcess.ProcessState.Running

    def write(self, data: bytes) -> None:
        if self.process.state() != QProcess.ProcessState.Running:
            raise RuntimeError("Codex app-server is not running")
        self.process.write(data)

    def set_secret_env_value(self, value: str | None) -> None:
        """Set an in-memory environment override; never put it in argv or config."""

        self._secret_env_value = value if value else None

    def redact(self, text: str) -> str:
        secrets = [os.environ.get(self.env_key, "")] if self.env_key else []
        if self._secret_env_value:
            secrets.append(self._secret_env_value)
        return redact_sensitive(text, secrets)

    def stop(self, timeout_ms: int = 3000) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return
        log.info("Stopping Codex app-server")
        self.process.closeWriteChannel()
        graceful_ms = min(1500, timeout_ms)
        if self.process.waitForFinished(graceful_ms):
            return
        self.process.terminate()
        if not self.process.waitForFinished(max(0, timeout_ms - graceful_ms)):
            log.warning("App-server did not terminate; killing it")
            self.process.kill()
            self.process.waitForFinished(1000)

    def _read_stdout(self) -> None:
        self.stdout_received.emit(bytes(self.process.readAllStandardOutput()))

    def _read_stderr(self) -> None:
        text = bytes(self.process.readAllStandardError()).decode("utf-8", "replace").rstrip()
        if text:
            safe_text = self.redact(text)
            log.warning("app-server stderr: %s", safe_text)
            self.stderr_received.emit(safe_text)

    def _started(self) -> None:
        log.info("Codex app-server started (pid=%s)", self.process.processId())
        self.started.emit()

    def _finished(self, code: int, status: QProcess.ExitStatus) -> None:
        log.info("Codex app-server exited code=%s status=%s", code, status)
        self.stopped.emit(code, int(status.value))

    def _error(self, error: QProcess.ProcessError) -> None:
        message = self.process.errorString()
        log.error("Codex app-server process error %s: %s", error, message)
        self.failed.emit(message)
