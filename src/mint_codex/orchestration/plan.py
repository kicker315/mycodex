from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from mint_codex.models.orchestration import OrchestrationNode, OrchestrationNodeStatus

MAX_PLAN_NODES = 64
MAX_NODE_ID_LENGTH = 64
MAX_TITLE_LENGTH = 240
MAX_INSTRUCTIONS_LENGTH = 8000
MAX_CAPABILITY_LENGTH = 160
_NODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FORBIDDEN_NODE_FIELDS = frozenset(
    {"cwd", "path", "worktree", "worktree_path", "branch", "branch_name", "absolute_path", "base_commit"}
)
_ALLOWED_NODE_FIELDS = frozenset(
    {"node_id", "title", "instructions", "dependencies", "provider_id", "model_id", "capability_hint"}
)


@dataclass(frozen=True)
class PlanValidation:
    nodes: tuple[OrchestrationNode, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors and bool(self.nodes)


def parse_plan_payload(payload: object) -> PlanValidation:
    if not isinstance(payload, dict):
        return PlanValidation(errors=("Plan must be a JSON object",))
    unknown_top_level = set(payload) - {"nodes", "objective", "plan_version"}
    if unknown_top_level:
        return PlanValidation(errors=(f"Plan contains unknown fields: {', '.join(sorted(unknown_top_level))}",))
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list):
        return PlanValidation(errors=("Plan nodes must be an array",))
    if not raw_nodes:
        return PlanValidation(errors=("Plan must contain at least one node",))
    if len(raw_nodes) > MAX_PLAN_NODES:
        return PlanValidation(errors=(f"Plan exceeds the {MAX_PLAN_NODES}-node safety limit",))

    nodes: list[OrchestrationNode] = []
    errors: list[str] = []
    seen: set[str] = set()
    for position, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            errors.append(f"Node {position + 1} must be an object")
            continue
        unknown = set(raw) - _ALLOWED_NODE_FIELDS
        forbidden = unknown & _FORBIDDEN_NODE_FIELDS
        if forbidden:
            errors.append(f"Node {position + 1} contains forbidden fields: {', '.join(sorted(forbidden))}")
        elif unknown:
            errors.append(f"Node {position + 1} contains unknown fields: {', '.join(sorted(unknown))}")

        node_id = raw.get("node_id")
        title = raw.get("title")
        instructions = raw.get("instructions")
        if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id):
            errors.append(f"Node {position + 1} has an invalid node_id")
            continue
        if node_id in seen:
            errors.append(f"Duplicate node_id: {node_id}")
        seen.add(node_id)
        if not isinstance(title, str) or not (title := " ".join(title.split())):
            errors.append(f"Node {node_id} title cannot be empty")
            title = node_id
        if len(title) > MAX_TITLE_LENGTH:
            errors.append(f"Node {node_id} title exceeds {MAX_TITLE_LENGTH} characters")
        if not isinstance(instructions, str) or not (instructions := instructions.strip()):
            errors.append(f"Node {node_id} instructions cannot be empty")
            instructions = "(invalid)"
        if len(instructions) > MAX_INSTRUCTIONS_LENGTH:
            errors.append(f"Node {node_id} instructions exceed {MAX_INSTRUCTIONS_LENGTH} characters")

        dependencies = raw.get("dependencies", [])
        if not isinstance(dependencies, list) or not all(isinstance(value, str) for value in dependencies):
            errors.append(f"Node {node_id} dependencies must be an array of node_id strings")
            dependencies = []
        dependencies = tuple(dependencies)
        if len(set(dependencies)) != len(dependencies):
            errors.append(f"Node {node_id} contains duplicate dependencies")
        provider_id = raw.get("provider_id")
        if provider_id is not None and (not isinstance(provider_id, str) or not provider_id.strip()):
            errors.append(f"Node {node_id} provider_id must be a non-empty string")
            provider_id = None
        model_id = raw.get("model_id")
        if model_id is not None and (not isinstance(model_id, str) or not model_id.strip()):
            errors.append(f"Node {node_id} model_id must be a non-empty string")
            model_id = None
        capability_hint = raw.get("capability_hint")
        if capability_hint is not None and not isinstance(capability_hint, str):
            errors.append(f"Node {node_id} capability_hint must be a string")
            capability_hint = None
        if isinstance(capability_hint, str) and len(capability_hint) > MAX_CAPABILITY_LENGTH:
            errors.append(f"Node {node_id} capability_hint exceeds {MAX_CAPABILITY_LENGTH} characters")
        nodes.append(
            OrchestrationNode(
                node_id=node_id,
                run_id="",
                title=title,
                instructions=instructions,
                dependencies=dependencies,
                provider_id=provider_id.strip() if isinstance(provider_id, str) else None,
                model_id=model_id.strip() if isinstance(model_id, str) else None,
                capability_hint=capability_hint.strip() if isinstance(capability_hint, str) else None,
            )
        )
    graph_errors = _graph_errors(nodes)
    errors.extend(graph_errors)
    return PlanValidation(tuple(nodes), tuple(dict.fromkeys(errors)))


def validate_plan(
    nodes: Iterable[OrchestrationNode],
    *,
    provider_ids: Iterable[str],
    known_models: Mapping[str, Iterable[str]] | None = None,
    max_nodes: int = MAX_PLAN_NODES,
) -> PlanValidation:
    normalized = tuple(nodes)
    errors: list[str] = []
    if not normalized:
        errors.append("Plan must contain at least one node")
    if len(normalized) > max_nodes:
        errors.append(f"Plan exceeds the {max_nodes}-node safety limit")
    seen: set[str] = set()
    providers = set(provider_ids)
    model_catalog = {provider: set(models) for provider, models in (known_models or {}).items()}
    for node in normalized:
        if node.node_id in seen:
            errors.append(f"Duplicate node_id: {node.node_id}")
        seen.add(node.node_id)
        if not _NODE_ID.fullmatch(node.node_id):
            errors.append(f"Node {node.node_id!r} has an invalid node_id")
        if not node.title.strip():
            errors.append(f"Node {node.node_id} title cannot be empty")
        if not node.instructions.strip():
            errors.append(f"Node {node.node_id} instructions cannot be empty")
        if node.provider_id is not None and node.provider_id not in providers:
            errors.append(f"Node {node.node_id} references unknown Provider {node.provider_id}")
        if node.model_id:
            if not node.provider_id:
                errors.append(f"Node {node.node_id} cannot select a Model without a Provider")
            elif not model_catalog.get(node.provider_id):
                errors.append(f"Node {node.node_id} Model catalog is unavailable for Provider {node.provider_id}")
            elif node.model_id not in model_catalog[node.provider_id]:
                errors.append(f"Node {node.node_id} references unknown Model {node.model_id}")
    errors.extend(_graph_errors(normalized))
    return PlanValidation(normalized, tuple(dict.fromkeys(errors)))


def _graph_errors(nodes: Iterable[OrchestrationNode]) -> list[str]:
    nodes = tuple(nodes)
    identifiers = {node.node_id for node in nodes}
    errors: list[str] = []
    for node in nodes:
        if node.node_id in node.dependencies:
            errors.append(f"Node {node.node_id} cannot depend on itself")
        for dependency in node.dependencies:
            if dependency not in identifiers:
                errors.append(f"Node {node.node_id} depends on missing node {dependency}")
    indegree = {node.node_id: 0 for node in nodes}
    dependents: dict[str, list[str]] = {node.node_id: [] for node in nodes}
    for node in nodes:
        for dependency in node.dependencies:
            if dependency in indegree:
                indegree[node.node_id] += 1
                dependents[dependency].append(node.node_id)
    queue = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    visited = 0
    while queue:
        current = queue.pop(0)
        visited += 1
        for dependent in sorted(dependents[current]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
                queue.sort()
    if visited != len(nodes):
        errors.append("Plan contains a dependency cycle")
    return errors
