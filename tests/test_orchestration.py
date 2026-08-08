from __future__ import annotations

import pytest
import subprocess
from pathlib import Path

from mint_codex.models.orchestration import (
    OrchestrationNode,
    OrchestrationNodeStatus,
    OrchestrationRun,
    OrchestrationRunStatus,
    ResultEnvelope,
)
from mint_codex.models.agent_task import AgentTask
from mint_codex.models.session import Item
from mint_codex.orchestration.plan import parse_plan_payload, validate_plan
from mint_codex.orchestration.scheduler import OrchestrationScheduler
from mint_codex.workspace.registry import ProjectRegistry

from test_agent_tasks import FakeBackend, _controller, _git_project


def _node(node_id: str, *dependencies: str, status=OrchestrationNodeStatus.PENDING):
    return OrchestrationNode(
        node_id=node_id,
        run_id="run-1",
        title=node_id.upper(),
        instructions=f"Do {node_id}",
        dependencies=tuple(dependencies),
        status=status,
    )


def test_plan_parser_accepts_result_only_dag_and_rejects_paths():
    parsed = parse_plan_payload(
        {
            "nodes": [
                {"node_id": "a", "title": "A", "instructions": "Inspect", "dependencies": []},
                {"node_id": "b", "title": "B", "instructions": "Test", "dependencies": []},
                {"node_id": "c", "title": "C", "instructions": "Summarize", "dependencies": ["a", "b"]},
            ]
        }
    )
    assert parsed.valid
    assert parsed.nodes[-1].dependencies == ("a", "b")
    assert parsed.nodes[-1].agent_task_id is None

    unsafe = parse_plan_payload(
        {"nodes": [{"node_id": "a", "title": "A", "instructions": "x", "cwd": "/tmp"}]}
    )
    assert not unsafe.valid
    assert any("cwd" in error for error in unsafe.errors)


@pytest.mark.parametrize(
    "nodes, expected",
    [
        ([{"node_id": "a", "title": "A", "instructions": "x", "dependencies": ["missing"]}], "missing"),
        (
            [
                {"node_id": "a", "title": "A", "instructions": "x", "dependencies": ["a"]},
            ],
            "itself",
        ),
        (
            [
                {"node_id": "a", "title": "A", "instructions": "x", "dependencies": ["b"]},
                {"node_id": "b", "title": "B", "instructions": "x", "dependencies": ["a"]},
            ],
            "cycle",
        ),
    ],
)
def test_plan_validation_rejects_invalid_dependency_graph(nodes, expected):
    parsed = parse_plan_payload({"nodes": nodes})
    assert not parsed.valid
    assert any(expected in error.lower() for error in parsed.errors)


def test_plan_validation_rejects_duplicate_empty_and_unknown_provider():
    duplicate = parse_plan_payload(
        {
            "nodes": [
                {"node_id": "a", "title": "A", "instructions": "x"},
                {"node_id": "a", "title": "A2", "instructions": "y"},
            ]
        }
    )
    assert not duplicate.valid
    assert any("duplicate" in error.lower() for error in duplicate.errors)

    empty = parse_plan_payload({"nodes": []})
    assert not empty.valid

    node = parse_plan_payload(
        {"nodes": [{"node_id": "a", "title": "A", "instructions": "x", "provider_id": "missing"}]}
    ).nodes
    validated = validate_plan(node, provider_ids={"openai"})
    assert not validated.valid
    assert any("provider" in error.lower() for error in validated.errors)

    unknown_model = parse_plan_payload(
        {
            "nodes": [
                {
                    "node_id": "a",
                    "title": "A",
                    "instructions": "x",
                    "provider_id": "openai",
                    "model_id": "not-advertised",
                }
            ]
        }
    ).nodes
    validated_model = validate_plan(unknown_model, provider_ids={"openai"}, known_models={"openai": set()})
    assert not validated_model.valid
    assert any("model" in error.lower() for error in validated_model.errors)


def test_approved_run_freezes_plan():
    run = OrchestrationRun(
        run_id="run-1",
        project_id="project-1",
        objective="Ship the feature",
        base_commit="a" * 40,
        default_provider_id="openai",
        default_model_id=None,
        max_parallelism=2,
    )
    run.transition_to(OrchestrationRunStatus.VALIDATED)
    run.transition_to(OrchestrationRunStatus.APPROVED)
    assert run.plan_frozen
    with pytest.raises(ValueError):
        run.transition_to(OrchestrationRunStatus.DRAFT)


def test_scheduler_starts_independent_nodes_and_blocks_dependents():
    scheduler = OrchestrationScheduler()
    nodes = [_node("a"), _node("b"), _node("c", "a", "b")]
    decision = scheduler.decide(nodes, max_parallelism=2)
    assert decision.start_node_ids == ("a", "b")
    assert decision.blocked_node_ids == ()

    nodes[0].status = OrchestrationNodeStatus.FAILED
    decision = scheduler.decide(nodes, max_parallelism=2, running_node_ids=set())
    assert decision.blocked_node_ids == ("c",)


def test_scheduler_pause_and_capacity_are_deterministic():
    scheduler = OrchestrationScheduler()
    nodes = [_node("b"), _node("a"), _node("c")]
    assert scheduler.decide(nodes, max_parallelism=2, paused=True).start_node_ids == ()
    decision = scheduler.decide(nodes, max_parallelism=2, running_node_ids={"a"})
    assert decision.start_node_ids == ("b",)


def test_result_envelope_is_bounded_and_prompt_is_result_only():
    result = ResultEnvelope(
        node_id="a",
        agent_task_id="task-a",
        terminal_status="completed",
        final_message="summary",
        changed_files=("src/a.py",),
        branch_name="mint-codex/project/task-a",
        worktree_reference="/home/user/.local/share/mint-codex/worktrees/project/task-a",
    )
    prompt = result.prompt_context()
    assert "summary" in prompt
    assert "src/a.py" in prompt
    assert result.branch_name not in prompt
    assert result.worktree_reference not in prompt


def test_registry_persists_run_nodes_and_task_mapping_atomically(tmp_path):
    project_root = tmp_path / "repo"
    project_root.mkdir()
    registry = ProjectRegistry(tmp_path / "workspace.sqlite3")
    project = registry.add_project(project_root, name="repo")
    run = OrchestrationRun(
        run_id="run-1",
        project_id=project.id,
        objective="objective",
        base_commit="b" * 40,
        default_provider_id="openai",
        default_model_id=None,
        max_parallelism=2,
    )
    node = _node("a")
    node.run_id = run.run_id
    node.bind_task("task-a")
    registry.upsert_orchestration(run, [node])

    restored_run, restored_nodes = registry.get_orchestration(run.run_id)
    assert restored_run is not None
    assert restored_run.base_commit == "b" * 40
    assert restored_nodes[0].agent_task_id == "task-a"
    registry.close()


def test_registry_persists_agent_task_base_commit(tmp_path):
    registry = ProjectRegistry(tmp_path / "workspace.sqlite3")
    project = registry.add_project(tmp_path, name="repo")
    task = AgentTask(
        id="task-a",
        project_id=project.id,
        title="A",
        task_prompt="x",
        provider_id="openai",
        model_id=None,
        reasoning_effort=None,
        thread_id=None,
        worktree_path=tmp_path / "worktree",
        branch_name="mint-codex/project/task-a",
        base_commit="c" * 40,
    )
    registry.upsert_agent_task(task)
    assert registry.get_agent_task(task.id).base_commit == "c" * 40
    registry.close()


def test_approved_run_schedules_parallel_workers_and_result_only_successor(qapp, tmp_path):
    project = tmp_path / "repo"
    _git_project(project)
    controller = _controller(tmp_path, project)
    run = controller.create_orchestration_run("Implement the objective", max_parallelism=2)
    assert run is not None
    payload = {
        "nodes": [
            {"node_id": "a", "title": "A", "instructions": "Do A", "dependencies": []},
            {"node_id": "b", "title": "B", "instructions": "Do B", "dependencies": []},
            {"node_id": "c", "title": "C", "instructions": "Use results", "dependencies": ["a", "b"]},
        ]
    }
    assert controller.set_orchestration_plan(run.run_id, payload).valid
    assert controller.validate_orchestration_run(run.run_id).valid
    assert controller.approve_orchestration_run(run.run_id)
    assert not [request for request in controller.backends["openai"].requests if request[0] == "thread/start"]
    assert controller.start_orchestration_run(run.run_id)

    nodes = {node.node_id: node for node in controller.orchestration_nodes(run.run_id)}
    task_a = controller.agent_task(nodes["a"].agent_task_id)
    task_b = controller.agent_task(nodes["b"].agent_task_id)
    assert task_a is not None and task_b is not None
    assert task_a.base_commit == run.base_commit == task_b.base_commit
    assert task_a.worktree_path != task_b.worktree_path
    assert controller.active_workspace_context.kind.value == "main_workspace"
    assert nodes["c"].agent_task_id is None
    original_worker_ids = {nodes["a"].agent_task_id, nodes["b"].agent_task_id}
    controller._schedule_orchestration(run.run_id)
    assert {
        node.agent_task_id
        for node in controller.orchestration_nodes(run.run_id)
        if node.agent_task_id
    } == original_worker_ids

    for task, message in ((task_a, "A result"), (task_b, "B result")):
        runtime = controller._agent_runtimes[task.id]
        runtime.items["message"] = Item("message", "agentMessage", {}, text=message)
        controller._on_backend_notification(
            task.provider_id,
            "turn/completed",
            {"threadId": task.thread_id, "turn": {"id": task.active_turn_id, "status": "completed"}},
        )

    nodes = {node.node_id: node for node in controller.orchestration_nodes(run.run_id)}
    assert nodes["a"].status == OrchestrationNodeStatus.SUCCEEDED
    assert nodes["b"].status == OrchestrationNodeStatus.SUCCEEDED
    task_c = controller.agent_task(nodes["c"].agent_task_id)
    assert task_c is not None
    assert "A result" in task_c.task_prompt and "B result" in task_c.task_prompt
    assert str(task_a.worktree_path) not in task_c.task_prompt
    assert task_a.branch_name not in task_c.task_prompt
    assert nodes["c"].status in {OrchestrationNodeStatus.STARTING, OrchestrationNodeStatus.RUNNING}


def test_failed_node_blocks_only_dependents_and_pause_does_not_stop_workers(qapp, tmp_path):
    project = tmp_path / "repo"
    _git_project(project)
    controller = _controller(tmp_path, project)
    run = controller.create_orchestration_run("Failure propagation", max_parallelism=2)
    assert run is not None
    payload = {
        "nodes": [
            {"node_id": "a", "title": "A", "instructions": "fail", "dependencies": []},
            {"node_id": "b", "title": "B", "instructions": "continue", "dependencies": []},
            {"node_id": "c", "title": "C", "instructions": "blocked", "dependencies": ["a"]},
        ]
    }
    assert controller.set_orchestration_plan(run.run_id, payload).valid
    assert controller.approve_orchestration_run(run.run_id)
    assert controller.start_orchestration_run(run.run_id)
    nodes = {node.node_id: node for node in controller.orchestration_nodes(run.run_id)}
    task_a = controller.agent_task(nodes["a"].agent_task_id)
    task_b = controller.agent_task(nodes["b"].agent_task_id)
    assert task_a is not None and task_b is not None
    assert controller.pause_orchestration_run(run.run_id)
    assert task_b.is_running
    controller._on_backend_notification(
        task_a.provider_id,
        "turn/completed",
        {
            "threadId": task_a.thread_id,
            "turn": {"id": task_a.active_turn_id, "status": "failed", "error": {"message": "boom"}},
        },
    )
    nodes = {node.node_id: node for node in controller.orchestration_nodes(run.run_id)}
    assert nodes["a"].status == OrchestrationNodeStatus.FAILED
    assert nodes["c"].status == OrchestrationNodeStatus.BLOCKED
    assert nodes["b"].status == OrchestrationNodeStatus.RUNNING
    assert controller.orchestration_run(run.run_id).status == OrchestrationRunStatus.PAUSED


def test_cancel_node_and_cancel_run_are_scoped_without_stopping_provider(qapp, tmp_path):
    project = tmp_path / "repo"
    _git_project(project)
    controller = _controller(tmp_path, project)
    run = controller.create_orchestration_run("Cancellation", max_parallelism=1)
    assert run is not None
    assert controller.set_orchestration_plan(
        run.run_id,
        {
            "nodes": [
                {"node_id": "a", "title": "A", "instructions": "A", "dependencies": []},
                {"node_id": "b", "title": "B", "instructions": "B", "dependencies": []},
            ]
        },
    ).valid
    assert controller.approve_orchestration_run(run.run_id)
    assert controller.start_orchestration_run(run.run_id)
    nodes = {node.node_id: node for node in controller.orchestration_nodes(run.run_id)}
    assert nodes["b"].status == OrchestrationNodeStatus.PENDING
    assert controller.cancel_orchestration_node(run.run_id, "a")
    assert controller.orchestration_nodes(run.run_id)[0].status in {
        OrchestrationNodeStatus.CANCELLED,
        OrchestrationNodeStatus.BLOCKED,
    }
    assert controller.resume_orchestration_run(run.run_id) is False

    second = controller.create_orchestration_run("Cancel entire run", max_parallelism=2)
    assert second is not None
    assert controller.set_orchestration_plan(
        second.run_id,
        {"nodes": [{"node_id": "a", "title": "A", "instructions": "A", "dependencies": []}]},
    ).valid
    assert controller.approve_orchestration_run(second.run_id)
    assert controller.start_orchestration_run(second.run_id)
    backend = controller.backends["openai"]
    assert controller.cancel_orchestration_run(second.run_id)
    assert controller.orchestration_run(second.run_id).status == OrchestrationRunStatus.CANCELLED
    assert backend.is_running


def test_restart_marks_active_run_recovery_required_without_rescheduling(qapp, tmp_path):
    project = tmp_path / "repo"
    _git_project(project)
    controller = _controller(tmp_path, project)
    run = controller.create_orchestration_run("Recover", max_parallelism=1)
    assert run is not None
    assert controller.set_orchestration_plan(
        run.run_id,
        {"nodes": [{"node_id": "a", "title": "A", "instructions": "A", "dependencies": []}]},
    ).valid
    assert controller.approve_orchestration_run(run.run_id)
    assert controller.start_orchestration_run(run.run_id)
    task_id = controller.orchestration_nodes(run.run_id)[0].agent_task_id
    assert task_id is not None
    controller.registry.close()

    restored = _controller(tmp_path, project)
    recovered = restored.orchestration_run(run.run_id)
    assert recovered is not None
    assert recovered.status == OrchestrationRunStatus.RECOVERY_REQUIRED
    assert restored.orchestration_nodes(run.run_id)[0].agent_task_id == task_id
    assert not [
        request
        for request in restored.backends["openai"].requests
        if request[0] in {"thread/start", "thread/resume", "turn/start"}
    ]
    assert restored.resume_orchestration_run(run.run_id)
    assert len(restored.backends["openai"].requests) > 0
    restored.registry.close()


def test_planner_is_an_isolated_read_only_draft_producer_and_never_approves_run(qapp, tmp_path):
    project = tmp_path / "repo"
    _git_project(project)
    controller = _controller(tmp_path, project)
    run = controller.create_orchestration_run("Plan this safely")
    assert run is not None
    planner = controller.generate_orchestration_plan(run.run_id)
    assert planner is not None
    assert planner.base_commit == run.base_commit
    assert run.status == OrchestrationRunStatus.DRAFT
    assert controller.orchestration_nodes(run.run_id) == []
    runtime = controller._agent_runtimes[planner.id]
    runtime.items["message"] = Item(
        "message",
        "agentMessage",
        {},
        text='{"nodes":[{"node_id":"a","title":"A","instructions":"Inspect","dependencies":[]}]}'
    )
    controller._on_backend_notification(
        planner.provider_id,
        "turn/completed",
        {"threadId": planner.thread_id, "turn": {"id": planner.active_turn_id, "status": "completed"}},
    )
    assert run.status == OrchestrationRunStatus.DRAFT
    assert [node.node_id for node in controller.orchestration_nodes(run.run_id)] == ["a"]
    assert not controller.start_orchestration_run(run.run_id)
