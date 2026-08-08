from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from mint_codex.models.orchestration import OrchestrationNode, OrchestrationNodeStatus


@dataclass(frozen=True)
class SchedulerDecision:
    start_node_ids: tuple[str, ...] = ()
    blocked_node_ids: tuple[str, ...] = ()


class OrchestrationScheduler:
    """Deterministic, side-effect-free scheduling decisions for a valid DAG."""

    def decide(
        self,
        nodes: Iterable[OrchestrationNode],
        *,
        max_parallelism: int,
        running_node_ids: set[str] | None = None,
        paused: bool = False,
    ) -> SchedulerDecision:
        node_map = {node.node_id: node for node in nodes}
        running = running_node_ids or set()
        blocked: list[str] = []
        candidates: list[str] = []
        for node_id in sorted(node_map):
            node = node_map[node_id]
            if node.status not in {OrchestrationNodeStatus.PENDING, OrchestrationNodeStatus.READY}:
                continue
            dependencies = [node_map[dependency] for dependency in node.dependencies if dependency in node_map]
            if any(dependency.status in {
                OrchestrationNodeStatus.FAILED,
                OrchestrationNodeStatus.CANCELLED,
                OrchestrationNodeStatus.BLOCKED,
            } for dependency in dependencies):
                blocked.append(node_id)
                continue
            if all(dependency.status == OrchestrationNodeStatus.SUCCEEDED for dependency in dependencies):
                if node_id not in running:
                    candidates.append(node_id)
        if paused:
            return SchedulerDecision(blocked_node_ids=tuple(blocked))
        capacity = max(0, int(max_parallelism) - len(running))
        return SchedulerDecision(
            start_node_ids=tuple(candidates[:capacity]),
            blocked_node_ids=tuple(blocked),
        )
