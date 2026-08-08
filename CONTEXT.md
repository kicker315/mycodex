# Mint Codex Desktop Context

This context describes supervised orchestration of isolated Codex Agent Tasks. It keeps planning and dependency coordination distinct from the existing AgentTask execution plane.

## Orchestration

**Orchestration Run**:
A user-owned execution record for one approved objective and its immutable plan revision.
_Avoid_: workflow, autonomous run

**Plan Node**:
A single user-approved unit of work in an Orchestration Run, with explicit dependencies and an optional AgentTask binding.
_Avoid_: worker, step (when referring to the plan definition)

**Worker AgentTask**:
The MVP-5A isolated execution context that performs a Plan Node in its own Thread and Git Worktree.
_Avoid_: orchestration node, planner task

**Result-only dependency**:
A dependency in which a downstream Plan Node receives an immutable summary of an upstream result, but not its unmerged files or Worktree state.
_Avoid_: code dependency, inherited Worktree

**Plan approval**:
The explicit user action that freezes a validated Plan Draft and permits scheduling.
_Avoid_: planner approval, automatic approval

## Workspace safety

**Base commit**:
The repository commit fixed for an Orchestration Run before any Worker AgentTask is created.
_Avoid_: moving HEAD, live branch tip

**Main Workspace**:
The Project's user-controlled working tree; an Orchestration Run never uses it as a Worker cwd.
_Avoid_: shared Worktree
