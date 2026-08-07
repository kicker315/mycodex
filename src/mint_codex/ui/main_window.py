from __future__ import annotations

import html

from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QMainWindow, QPushButton,
    QTextBrowser, QTextEdit, QVBoxLayout, QWidget,
)

from mint_codex.core.controller import CodexController


class MainWindow(QMainWindow):
    def __init__(self, controller: CodexController):
        super().__init__()
        self.controller = controller
        self.setWindowTitle("Mint Codex Desktop")
        self.resize(900, 680)
        root = QWidget()
        layout = QVBoxLayout(root)
        top = QHBoxLayout()
        self.connection = QLabel("Disconnected")
        self.account = QLabel("")
        self.models = QComboBox()
        self.models.setEnabled(False)
        top.addWidget(self.connection)
        top.addStretch()
        top.addWidget(self.account)
        top.addWidget(QLabel("Model:"))
        top.addWidget(self.models)
        layout.addLayout(top)
        self.timeline = QTextBrowser()
        self.timeline.setOpenExternalLinks(False)
        layout.addWidget(self.timeline, 1)
        bottom = QHBoxLayout()
        self.input = QTextEdit()
        self.input.setPlaceholderText("Ask Codex…")
        self.input.setMaximumHeight(110)
        self.send = QPushButton("Send")
        self.send.clicked.connect(self._send)
        bottom.addWidget(self.input, 1)
        bottom.addWidget(self.send)
        layout.addLayout(bottom)
        self.setCentralWidget(root)
        controller.connection_changed.connect(self.connection.setText)
        controller.account_changed.connect(self.account.setText)
        controller.models_changed.connect(self._set_models)
        controller.timeline_event.connect(self._event)
        controller.busy_changed.connect(lambda busy: self.send.setEnabled(not busy))
        controller.error.connect(self._error)
        self.models.currentIndexChanged.connect(self._model_changed)

    def _set_models(self, models: object, default: object) -> None:
        self.models.blockSignals(True)
        self.models.clear()
        default_index = 0
        for index, model in enumerate(models if isinstance(models, list) else []):
            if not isinstance(model, dict):
                continue
            model_id = model.get("model")
            if not isinstance(model_id, str):
                continue
            self.models.addItem(str(model.get("displayName", model_id)), model_id)
            if model_id == default:
                default_index = self.models.count() - 1
        self.models.setCurrentIndex(default_index)
        self.models.setEnabled(self.models.count() > 0)
        self.models.blockSignals(False)
        self._model_changed(default_index)

    def _model_changed(self, index: int) -> None:
        self.controller.set_model(self.models.itemData(index) if index >= 0 else None)

    def _send(self) -> None:
        text = self.input.toPlainText().strip()
        if text:
            self.input.clear()
            self.controller.send_text(text)

    def _event(self, kind: str, data: object) -> None:
        data = data if isinstance(data, dict) else {}
        if kind == "user":
            self.timeline.append(f"<p><b>You</b><br>{html.escape(str(data.get('text', '')))}</p>")
        elif kind == "thread":
            self.timeline.append(f"<p style='color:#777'>Thread {html.escape(str(data.get('id', '')))}</p>")
        elif kind == "agent_delta":
            cursor = self.timeline.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            cursor.insertText(str(data.get("delta", "")))
            self.timeline.setTextCursor(cursor)
        elif kind == "item_started" and data.get("type") != "agentMessage":
            self.timeline.append(f"<p style='color:#666'>Item started: {html.escape(str(data.get('type', 'unknown')))}</p>")
        elif kind == "turn_completed":
            status = html.escape(str(data.get("status", "completed")))
            self.timeline.append(f"<p style='color:#777'>Turn {status}</p>")
        elif kind == "turn_failed":
            self.timeline.append(
                f"<p style='color:#a33'><b>Codex turn failed</b><br>{html.escape(str(data.get('message', 'unknown error')))}</p>"
            )
        elif kind == "error":
            self.timeline.append(f"<p style='color:#a33'>Codex error: {html.escape(str(data.get('message', 'unknown error')))}</p>")
        elif kind == "unsupported_request":
            self.timeline.append(f"<p style='color:#a33'>Unsupported request safely rejected: {html.escape(str(data.get('method', '')))}</p>")

    def _error(self, message: str) -> None:
        self.statusBar().showMessage(message, 10000)

    def closeEvent(self, event) -> None:
        self.controller.stop()
        event.accept()
