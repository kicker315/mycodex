from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from mint_codex.models.session import Thread
from mint_codex.models.timeline import ApprovalRequest, TimelineItem, TimelineKind


class TimelineRenderer:
    """Render one domain TimelineItem without exposing protocol JSON to the UI."""

    _labels = {
        TimelineKind.USER_MESSAGE: "You",
        TimelineKind.AGENT_MESSAGE: "Agent",
        TimelineKind.COMMAND_EXECUTION: "Command execution",
        TimelineKind.FILE_CHANGE: "File change",
        TimelineKind.TOOL_CALL: "Tool activity",
        TimelineKind.APPROVAL_REQUEST: "Approval required",
        TimelineKind.STATUS: "Status",
        TimelineKind.ERROR: "Error",
    }

    @classmethod
    def render(cls, item: TimelineItem, parent: QWidget | None = None) -> QFrame:
        card = QFrame(parent)
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        card.setProperty("timelineKind", item.kind.value)
        card.setStyleSheet(cls._style(item.kind))
        layout = QVBoxLayout(card)
        heading = QLabel(cls._labels[item.kind])
        heading.setProperty("timelineRole", "heading")
        layout.addWidget(heading)

        if item.kind == TimelineKind.COMMAND_EXECUTION:
            metadata = item.metadata
            command = metadata.get("command") or item.text
            body_text = f"$ {command}"
            cwd = metadata.get("cwd")
            if cwd:
                body_text += f"\ncwd: {cwd}"
            output = metadata.get("aggregated_output")
            if output:
                body_text += f"\n\n{output}"
            if item.status:
                body_text += f"\nstatus: {item.status}"
            exit_code = metadata.get("exit_code")
            if exit_code is not None:
                body_text += f"\nexit code: {exit_code}"
        elif item.kind == TimelineKind.FILE_CHANGE:
            changes = item.metadata.get("changes", [])
            paths = []
            for change in changes if isinstance(changes, list) else []:
                if isinstance(change, dict) and isinstance(change.get("path"), str):
                    kind = change.get("kind")
                    kind_name = kind.get("type") if isinstance(kind, dict) else kind
                    paths.append(f"{kind_name}: {change['path']}" if kind_name else change["path"])
            body_text = "\n".join(paths) or item.text
        elif item.kind == TimelineKind.TOOL_CALL:
            body_text = item.text or "Tool activity"
        else:
            body_text = item.text or " "

        body = QLabel(body_text)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setProperty("timelineRole", "body")
        layout.addWidget(body)
        return card

    @staticmethod
    def _style(kind: TimelineKind) -> str:
        colors = {
            TimelineKind.USER_MESSAGE: ("#eef5ff", "#315d91"),
            TimelineKind.AGENT_MESSAGE: ("#f3f7f1", "#3e6c3b"),
            TimelineKind.ERROR: ("#fff0f0", "#9b3333"),
            TimelineKind.STATUS: ("#f5f5f5", "#666666"),
            TimelineKind.APPROVAL_REQUEST: ("#fff8e8", "#805d18"),
        }
        background, foreground = colors.get(kind, ("#fafafa", "#555555"))
        return (
            f"QFrame {{ background: {background}; border: 1px solid #d7d7d7; border-radius: 6px; }} "
            f"QLabel {{ color: {foreground}; }}"
        )


class FileChangeCard(QFrame):
    file_requested = Signal(str)

    def __init__(self, item: TimelineItem, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setProperty("timelineKind", TimelineKind.FILE_CHANGE.value)
        self.setStyleSheet(TimelineRenderer._style(TimelineKind.FILE_CHANGE))
        layout = QVBoxLayout(self)
        heading = QLabel("File change")
        heading.setProperty("timelineRole", "heading")
        layout.addWidget(heading)
        changes = item.metadata.get("changes", [])
        paths: list[str] = []
        for change in changes if isinstance(changes, list) else []:
            if isinstance(change, dict) and isinstance(change.get("path"), str):
                paths.append(change["path"])
        if not paths and item.text:
            paths = [item.text]
        for path in paths:
            button = QToolButton()
            button.setText(path)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, value=path: self.file_requested.emit(value))
            layout.addWidget(button)


class ApprovalCard(QFrame):
    decision_requested = Signal(str, str)

    _labels = {
        "accept": "Allow once",
        "acceptForSession": "Allow session",
        "decline": "Decline",
        "cancel": "Cancel",
    }

    def __init__(self, item: TimelineItem, parent: QWidget | None = None):
        super().__init__(parent)
        self.request_key = str(item.metadata.get("request_key", item.item_id or ""))
        self.setProperty("timelineKind", TimelineKind.APPROVAL_REQUEST.value)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(TimelineRenderer._style(TimelineKind.APPROVAL_REQUEST))
        layout = QVBoxLayout(self)
        heading = QLabel("Approval required")
        heading.setProperty("timelineRole", "heading")
        layout.addWidget(heading)
        command = item.metadata.get("command") or item.text
        body_text = str(command)
        cwd = item.metadata.get("cwd")
        reason = item.metadata.get("reason")
        if cwd:
            body_text += f"\ncwd: {cwd}"
        if reason and reason != command:
            body_text += f"\nreason: {reason}"
        body = QLabel(body_text or "Codex requested approval")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setProperty("timelineRole", "body")
        layout.addWidget(body)
        self._decision_label = QLabel("Waiting for your decision")
        layout.addWidget(self._decision_label)
        buttons = QHBoxLayout()
        allowed = item.metadata.get("allowed_decisions", ())
        for decision in allowed if isinstance(allowed, (list, tuple)) else ():
            if decision not in self._labels:
                continue
            button = QPushButton(self._labels[decision])
            button.setObjectName(f"approval_{decision}")
            button.clicked.connect(lambda _checked=False, value=decision: self._request(value))
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self._buttons = [button for button in self.findChildren(QPushButton)]

    def _request(self, decision: str) -> None:
        for button in self._buttons:
            button.setEnabled(False)
        self._decision_label.setText(f"Sending: {self._labels.get(decision, decision)}")
        self.decision_requested.emit(self.request_key, decision)

    def set_resolved(self, decision: str) -> None:
        self._decision_label.setText(f"Decision: {self._labels.get(decision, decision)}")


class TimelineWidget(QScrollArea):
    approval_decision = Signal(str, str)
    file_change_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._body = QWidget()
        self._layout = QVBoxLayout(self._body)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._layout.addStretch(1)
        self.setWidget(self._body)
        self._items: dict[str, TimelineItem] = {}
        self._cards: dict[str, QFrame] = {}
        self._anonymous_id = 0

    def clear(self) -> None:
        for card in self._cards.values():
            card.deleteLater()
        self._cards.clear()
        self._items.clear()
        self._anonymous_id = 0

    def load_thread(self, thread: Thread) -> None:
        self.clear()
        for turn in thread.turns:
            for item in turn.items:
                if isinstance(item.data, dict):
                    self.add_item(TimelineItem.from_codex_item(item.data))

    def add_approval_request(self, request: object) -> None:
        if not isinstance(request, ApprovalRequest):
            return
        self.add_item(TimelineItem.from_approval(request))

    def resolve_approval(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        request = payload.get("request")
        decision = payload.get("decision")
        if not isinstance(request, ApprovalRequest) or not isinstance(decision, str):
            return
        card = self._cards.get(request.request_key)
        if isinstance(card, ApprovalCard):
            card.set_resolved(decision)

    def apply_event(self, kind: str, data: object) -> None:
        item = TimelineItem.from_event(kind, data)
        if item is None:
            return
        if item.item_id:
            previous = self._items.get(item.item_id)
            if previous is not None:
                item = self._merge(previous, item)
        self.add_item(item)

    @staticmethod
    def _merge(previous: TimelineItem, incoming: TimelineItem) -> TimelineItem:
        metadata = {**previous.metadata, **incoming.metadata}
        if incoming.metadata.get("delta") or incoming.metadata.get("output_delta"):
            text = previous.text + incoming.text
        elif incoming.metadata.get("progress") and incoming.text and previous.text:
            text = f"{previous.text}\n{incoming.text}"
        else:
            text = incoming.text or previous.text
        if incoming.metadata.get("output_delta") and incoming.kind == TimelineKind.COMMAND_EXECUTION:
            output = str(metadata.get("aggregated_output") or "")
            metadata["aggregated_output"] = output + incoming.text
        return replace(
            incoming,
            text=text,
            status=incoming.status or previous.status,
            metadata=metadata,
        )

    def add_item(self, item: TimelineItem) -> None:
        key = item.item_id
        if key is None:
            self._anonymous_id += 1
            key = f"anonymous-{self._anonymous_id}"
        old = self._cards.get(key)
        if old is not None and item.kind == TimelineKind.APPROVAL_REQUEST:
            return
        if item.kind == TimelineKind.APPROVAL_REQUEST:
            card = ApprovalCard(item, self._body)
            card.decision_requested.connect(self.approval_decision)
        elif item.kind == TimelineKind.FILE_CHANGE:
            card = FileChangeCard(item, self._body)
            card.file_requested.connect(self.file_change_requested)
        else:
            card = TimelineRenderer.render(item, self._body)
        if old is None:
            self._layout.insertWidget(max(0, self._layout.count() - 1), card)
        else:
            index = self._layout.indexOf(old)
            self._layout.insertWidget(index, card)
            old.deleteLater()
        self._items[key] = item
        self._cards[key] = card
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
