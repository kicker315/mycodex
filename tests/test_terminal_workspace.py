from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer

from mint_codex.workspace.terminal import TerminalSession


def _wait_until(qapp, predicate, timeout=3000):
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(timeout)
    deadline = QTimer()
    deadline.setSingleShot(False)
    deadline.timeout.connect(lambda: loop.quit() if predicate() else None)
    deadline.start(20)
    while not predicate() and timer.isActive():
        loop.exec()
    deadline.stop()
    timer.stop()
    return predicate()


def test_terminal_session_starts_in_project_and_accepts_input(qapp, tmp_path: Path):
    session = TerminalSession(tmp_path)
    output = []
    session.output.connect(output.append)
    session.start()
    session.send_input("pwd\n")

    assert _wait_until(qapp, lambda: str(tmp_path) in "".join(output))
    session.interrupt()
    session.send_input("exit\n")
    session.stop()
    assert session.is_running is False
