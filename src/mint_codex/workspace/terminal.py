from __future__ import annotations

import errno
import fcntl
import os
import signal
import termios
import warnings
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QSocketNotifier, QTimer, QObject, Signal


class TerminalSession(QObject):
    """One Linux PTY shell whose cwd is fixed at construction time."""

    output = Signal(str)
    state_changed = Signal(str)
    finished = Signal(int)
    error = Signal(str)

    def __init__(self, root: Path, parent: QObject | None = None):
        super().__init__(parent)
        self.root = root.expanduser().resolve(strict=False)
        self._pid: int | None = None
        self._fd: int | None = None
        self._notifier: QSocketNotifier | None = None
        self._wait_timer = QTimer(self)
        self._wait_timer.setInterval(50)
        self._wait_timer.timeout.connect(self._poll_exit)

    @property
    def is_running(self) -> bool:
        return self._pid is not None

    def start(self) -> bool:
        if self.is_running:
            return True
        if not self.root.is_dir():
            self.error.emit("Terminal Project path is unavailable")
            return False
        shell = os.environ.get("SHELL") or "/bin/bash"
        try:
            pid, fd = self._fork_pty(shell)
        except OSError as exc:
            self.error.emit(f"Could not start terminal: {exc}")
            return False
        self._pid = pid
        self._fd = fd
        os.set_blocking(fd, False)
        self._notifier = QSocketNotifier(fd, QSocketNotifier.Type.Read, self)
        self._notifier.activated.connect(self._read)
        self._wait_timer.start()
        self.state_changed.emit("Running")
        return True

    def _fork_pty(self, shell: str) -> tuple[int, int]:
        master_fd, slave_fd = os.openpty()
        # The child performs only fd/session setup and immediately execs. Qt
        # keeps the GUI event loop in the parent; Python 3.12 warns about any
        # fork from a multi-threaded process even for this PTY pattern.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=DeprecationWarning,
                message=r"This process .* is multi-threaded, use of fork\(\)",
            )
            pid = os.fork()
        if pid == 0:
            try:
                os.close(master_fd)
                os.setsid()
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                os.dup2(slave_fd, 0)
                os.dup2(slave_fd, 1)
                os.dup2(slave_fd, 2)
                if slave_fd > 2:
                    os.close(slave_fd)
                os.chdir(self.root)
                os.execvpe(shell, [shell, "-i"], os.environ.copy())
            except BaseException:
                os._exit(127)
        os.close(slave_fd)
        return pid, master_fd

    def send_input(self, text: str) -> bool:
        if self._fd is None or not self.is_running:
            return False
        try:
            os.write(self._fd, text.encode("utf-8"))
            return True
        except OSError as exc:
            self.error.emit(f"Terminal input failed: {exc}")
            return False

    def interrupt(self) -> None:
        if self._pid is None:
            return
        try:
            os.killpg(self._pid, signal.SIGINT)
        except ProcessLookupError:
            pass

    def stop(self) -> None:
        pid = self._pid
        if pid is None:
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        self._finish(-signal.SIGKILL, flush_notifier=True)

    def _read(self, _fd: int) -> None:
        if self._fd is None:
            return
        try:
            data = os.read(self._fd, 65536)
        except OSError as exc:
            if exc.errno not in {errno.EAGAIN, errno.EWOULDBLOCK, errno.EIO}:
                self.error.emit(f"Terminal output failed: {exc}")
            return
        if data:
            self.output.emit(data.decode("utf-8", "replace"))
        else:
            self._poll_exit()

    def _poll_exit(self) -> None:
        if self._pid is None:
            return
        try:
            pid, status = os.waitpid(self._pid, os.WNOHANG)
        except ChildProcessError:
            pid, status = self._pid, 0
        if pid == self._pid:
            code = os.waitstatus_to_exitcode(status) if status else 0
            self._finish(code)

    def _finish(self, code: int, *, flush_notifier: bool = False) -> None:
        if self._notifier is not None:
            notifier = self._notifier
            notifier.setEnabled(False)
            notifier.deleteLater()
            self._notifier = None
            if flush_notifier:
                QCoreApplication.sendPostedEvents(notifier, QEvent.Type.DeferredDelete)
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
        self._fd = None
        self._pid = None
        self._wait_timer.stop()
        self.state_changed.emit("Exited")
        self.finished.emit(code)
