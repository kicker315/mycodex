from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer

from mint_codex.ui.developer_workspace import DeveloperWorkspacePanel
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


def test_terminal_panel_does_not_render_ansi_control_sequences(qapp, tmp_path: Path):
    panel = DeveloperWorkspacePanel()
    panel.set_project("project", tmp_path)

    panel._terminal_output(
        "main:project",
        "\x1b[?2004h\x1b]0;yanhui@wang: ~/projects/openocta\x07"
        "\x1b[01;32myanhui\x1b[00m@wang\x1b[01;34m:\x1b[00m$ ls\r\n"
        "\x1b[01;34msrc\x1b[0m\r\n",
    )

    displayed = panel.terminal_output.toPlainText()
    assert "\x1b" not in displayed
    assert "\x07" not in displayed
    assert "yanhui@wang:" in displayed
    assert "src" in displayed
    panel.shutdown()


def test_terminal_output_filter_handles_sequences_split_across_reads():
    from mint_codex.workspace.terminal import TerminalOutputFilter

    output_filter = TerminalOutputFilter()

    assert output_filter.feed("before\x1b[01;") == "before"
    assert output_filter.feed("32mgreen\x1b]0;title") == "green"
    assert output_filter.feed("\x07after\r\n") == "after\n"
