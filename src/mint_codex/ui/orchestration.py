from __future__ import annotations

import json

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from mint_codex.models.orchestration import OrchestrationNode, OrchestrationRun, OrchestrationRunStatus
from mint_codex.orchestration.plan import PlanValidation
from mint_codex.workspace.controller import WorkspaceController


class OrchestrationDialog(QDialog):
    """Small human-supervised Plan Draft and Run control surface."""

    def __init__(
        self,
        controller: WorkspaceController,
        parent: QWidget | None = None,
        run_id: str | None = None,
    ):
        super().__init__(parent)
        self.controller = controller
        self.run_id = run_id
        self._editor_dirty = False
        self.setWindowTitle("Supervised Agent Orchestration")
        self.resize(760, 680)
        self._build_ui()
        self._connect_signals()
        if run_id:
            self._load_run(run_id)
        self._update_controls()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.objective_input = QPlainTextEdit()
        self.objective_input.setPlaceholderText("Overall objective")
        self.objective_input.setMaximumHeight(80)
        self.provider_input = QComboBox()
        for provider_id in self.controller.provider_ids:
            self.provider_input.addItem(self.controller.providers[provider_id].display_name, provider_id)
        selected_provider = self.controller.selected_provider
        selected_index = self.provider_input.findData(selected_provider)
        if selected_index >= 0:
            self.provider_input.setCurrentIndex(selected_index)
        self.model_input = QLineEdit(self.controller._selected_models.get(selected_provider) or "")
        self.parallel_input = QSpinBox()
        self.parallel_input.setRange(1, 8)
        self.parallel_input.setValue(2)
        form.addRow("Objective", self.objective_input)
        form.addRow("Default Provider", self.provider_input)
        form.addRow("Default Model", self.model_input)
        form.addRow("Max parallel", self.parallel_input)
        layout.addLayout(form)

        self.create_run = QPushButton("Create Run")
        self.generate_plan = QPushButton("Generate Plan Draft")
        self.validate_plan = QPushButton("Validate")
        self.approve_plan = QPushButton("Approve Plan")
        self.start_run = QPushButton("Start")
        self.pause_run = QPushButton("Pause Scheduling")
        self.resume_run = QPushButton("Resume Scheduling")
        self.cancel_run = QPushButton("Cancel Run")
        button_row = QHBoxLayout()
        for button in (
            self.create_run,
            self.generate_plan,
            self.validate_plan,
            self.approve_plan,
            self.start_run,
            self.pause_run,
            self.resume_run,
            self.cancel_run,
        ):
            button_row.addWidget(button)
        layout.addLayout(button_row)

        self.run_status = QLabel("No Run")
        self.run_status.setObjectName("orchestration_run_status")
        layout.addWidget(self.run_status)
        self.plan_editor = QPlainTextEdit()
        self.plan_editor.setPlaceholderText(
            '{"nodes":[{"node_id":"a","title":"Task A","instructions":"...","dependencies":[]}]}'
        )
        self.plan_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(QLabel("Plan Draft JSON — edit nodes, dependencies, Provider and Model"))
        layout.addWidget(self.plan_editor, 2)

        result_row = QHBoxLayout()
        self.node_selector = QComboBox()
        self.open_task = QPushButton("Open AgentTask Workspace")
        result_row.addWidget(QLabel("Node"))
        result_row.addWidget(self.node_selector, 1)
        result_row.addWidget(self.open_task)
        layout.addLayout(result_row)
        self.node_results = QPlainTextEdit()
        self.node_results.setReadOnly(True)
        self.node_results.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.node_results, 1)

    def _connect_signals(self) -> None:
        self.create_run.clicked.connect(self._create_run)
        self.generate_plan.clicked.connect(self._generate_plan)
        self.validate_plan.clicked.connect(self._validate_plan)
        self.approve_plan.clicked.connect(self._approve_plan)
        self.start_run.clicked.connect(self._start_run)
        self.pause_run.clicked.connect(self._pause_run)
        self.resume_run.clicked.connect(self._resume_run)
        self.cancel_run.clicked.connect(self._cancel_run)
        self.open_task.clicked.connect(self._open_task)
        self.plan_editor.textChanged.connect(lambda: setattr(self, "_editor_dirty", True))
        self.controller.orchestration_run_changed.connect(self._run_changed)
        self.controller.orchestration_node_changed.connect(self._nodes_changed)
        self.controller.orchestration_plan_ready.connect(self._plan_ready)

    def _create_run(self) -> None:
        run = self.controller.create_orchestration_run(
            self.objective_input.toPlainText(),
            default_provider_id=str(self.provider_input.currentData()),
            default_model_id=self.model_input.text().strip() or None,
            max_parallelism=self.parallel_input.value(),
        )
        if run is not None:
            self.run_id = run.run_id
            self._show_run(run)

    def _generate_plan(self) -> None:
        if self.run_id:
            self.controller.generate_orchestration_plan(self.run_id)

    def _payload_from_editor(self) -> object | None:
        try:
            return json.loads(self.plan_editor.toPlainText())
        except (TypeError, ValueError) as exc:
            self.run_status.setText(f"Invalid Plan JSON: {exc}")
            return None

    def _set_plan(self) -> PlanValidation | None:
        if not self.run_id:
            return None
        payload = self._payload_from_editor()
        if payload is None:
            return None
        validation = self.controller.set_orchestration_plan(self.run_id, payload)
        self._show_validation(validation)
        if validation.valid:
            self._editor_dirty = False
        return validation

    def _validate_plan(self) -> None:
        if not self.run_id:
            return
        if self._editor_dirty and self._set_plan() is None:
            return
        validation = self.controller.validate_orchestration_run(self.run_id)
        self._show_validation(validation)

    def _approve_plan(self) -> None:
        if self._editor_dirty and self._set_plan() is None:
            return
        if self.run_id:
            self.controller.approve_orchestration_run(self.run_id)

    def _start_run(self) -> None:
        if self.run_id:
            self.controller.start_orchestration_run(self.run_id)

    def _pause_run(self) -> None:
        if self.run_id:
            self.controller.pause_orchestration_run(self.run_id)

    def _resume_run(self) -> None:
        if self.run_id:
            self.controller.resume_orchestration_run(self.run_id)

    def _cancel_run(self) -> None:
        if self.run_id:
            self.controller.cancel_orchestration_run(self.run_id)

    def _open_task(self) -> None:
        node_id = self.node_selector.currentData()
        if not self.run_id or not isinstance(node_id, str):
            return
        nodes = {node.node_id: node for node in self.controller.orchestration_nodes(self.run_id)}
        node = nodes.get(node_id)
        if node is not None and node.agent_task_id:
            self.controller.activate_agent_task(node.agent_task_id, source="orchestration-dialog")

    def _load_run(self, run_id: str) -> None:
        run = self.controller.orchestration_run(run_id)
        if run is None:
            self.run_id = None
            return
        self._show_run(run)
        self._render_nodes(self.controller.orchestration_nodes(run_id))

    def _run_changed(self, run: object) -> None:
        if isinstance(run, OrchestrationRun) and run.run_id == self.run_id:
            self._show_run(run)

    def _nodes_changed(self, nodes: object) -> None:
        if not self.run_id or not isinstance(nodes, (list, tuple)):
            return
        typed = [node for node in nodes if isinstance(node, OrchestrationNode)]
        if typed and typed[0].run_id == self.run_id:
            if not self._editor_dirty:
                self._set_editor_nodes(typed)
            self._render_nodes(typed)

    def _plan_ready(self, payload: object) -> None:
        if not isinstance(payload, dict) or payload.get("run_id") != self.run_id:
            return
        nodes = payload.get("nodes", [])
        if isinstance(nodes, (list, tuple)):
            typed = [node for node in nodes if isinstance(node, OrchestrationNode)]
            self._set_editor_nodes(typed)
            self._render_nodes(typed)

    def _show_run(self, run: OrchestrationRun) -> None:
        self.run_id = run.run_id
        self.objective_input.setPlainText(run.objective)
        provider_index = self.provider_input.findData(run.default_provider_id)
        if provider_index >= 0:
            self.provider_input.setCurrentIndex(provider_index)
        self.model_input.setText(run.default_model_id or "")
        self.parallel_input.setValue(run.max_parallelism)
        self.run_status.setText(f"Run {run.run_id[:8]} · {run.status.value} · base {run.base_commit[:12]}")
        self._update_controls(run.status)

    def _set_editor_nodes(self, nodes: list[OrchestrationNode]) -> None:
        payload = {
            "nodes": [
                {
                    "node_id": node.node_id,
                    "title": node.title,
                    "instructions": node.instructions,
                    "dependencies": list(node.dependencies),
                    "provider_id": node.provider_id,
                    "model_id": node.model_id,
                    "capability_hint": node.capability_hint,
                }
                for node in nodes
            ]
        }
        blocker = QSignalBlocker(self.plan_editor)
        self.plan_editor.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
        del blocker
        self._editor_dirty = False

    def _render_nodes(self, nodes: list[OrchestrationNode]) -> None:
        blocker = QSignalBlocker(self.node_selector)
        self.node_selector.clear()
        for node in sorted(nodes, key=lambda item: item.node_id):
            self.node_selector.addItem(f"{node.node_id} · {node.status.value}", node.node_id)
        del blocker
        lines = []
        for node in sorted(nodes, key=lambda item: item.node_id):
            result = node.result.final_message[:500] if node.result else ""
            reason = node.blocked_reason or ""
            lines.append(f"{node.node_id}: {node.status.value} {reason}\n{result}".rstrip())
        self.node_results.setPlainText("\n\n".join(lines))
        self._update_controls()

    def _show_validation(self, validation: PlanValidation) -> None:
        if validation.valid:
            self.run_status.setText(f"Plan valid · {len(validation.nodes)} node(s)")
        else:
            self.run_status.setText("Plan invalid: " + "; ".join(validation.errors))

    def _update_controls(self, status: OrchestrationRunStatus | None = None) -> None:
        run = self.controller.orchestration_run(self.run_id) if self.run_id else None
        status = status or (run.status if run else None)
        has_run = run is not None
        draft = status in {OrchestrationRunStatus.DRAFT, OrchestrationRunStatus.VALIDATED}
        self.generate_plan.setEnabled(has_run and status == OrchestrationRunStatus.DRAFT)
        self.validate_plan.setEnabled(has_run and draft)
        self.approve_plan.setEnabled(has_run and status == OrchestrationRunStatus.VALIDATED)
        self.start_run.setEnabled(has_run and status == OrchestrationRunStatus.APPROVED)
        self.pause_run.setEnabled(has_run and status == OrchestrationRunStatus.RUNNING)
        self.resume_run.setEnabled(has_run and status in {OrchestrationRunStatus.PAUSED, OrchestrationRunStatus.RECOVERY_REQUIRED})
        self.cancel_run.setEnabled(
            has_run
            and status
            not in {
                OrchestrationRunStatus.COMPLETED,
                OrchestrationRunStatus.PARTIAL_FAILED,
                OrchestrationRunStatus.CANCELLED,
                OrchestrationRunStatus.FAILED,
            }
        )
        self.open_task.setEnabled(bool(self.node_selector.currentData()))
