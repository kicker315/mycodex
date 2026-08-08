from __future__ import annotations

from PySide6.QtCore import QProcess

from mint_codex.core.process import AppServerProcess


def test_write_forwards_rpc_payload_to_running_process(qapp, monkeypatch):
    app_server = AppServerProcess()
    payload = b'{"jsonrpc":"2.0","method":"initialize"}\n'
    written: list[bytes] = []

    monkeypatch.setattr(
        app_server.process,
        "state",
        lambda: QProcess.ProcessState.Running,
    )
    monkeypatch.setattr(app_server.process, "write", written.append)

    app_server.write(payload)

    assert written == [payload]
