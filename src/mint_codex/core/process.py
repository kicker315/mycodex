from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QProcess, Signal

from .redaction import redact_sensitive

log = logging.getLogger(__name__)


class AppServerProcess(QObject):
    stdout_received = Signal(bytes)
    stderr_received = Signal(str)
    started = Signal()
    stopped = Signal(int, int)
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
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
            self.process.start("codex", ["app-server", "--stdio"])

    @property
    def is_running(self) -> bool:
        return self.process.state() == QProcess.ProcessState.Running

    def write(self, data: bytes) -> None:
        if self.process.state() != QProcess.ProcessState.Running:
            raise RuntimeError("Codex app-server is not running")
        self.process.write(data)

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
            safe_text = redact_sensitive(text)
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
