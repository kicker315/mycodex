# Mint Codex Desktop

A small native Linux/PySide6 client for the installed official `codex app-server`.
It does not implement an agent loop or call OpenAI APIs directly.

Run with:

```bash
python -m mint_codex.main
```

Set `QT_QPA_PLATFORM=offscreen` for a headless GUI smoke test.

## MVP-2 providers

The desktop client owns a `ProviderRouter` with one independent Codex App Server process per provider:

- OpenAI uses the existing `~/.codex` and ChatGPT login.
- DeepSeek uses `~/.codex-deepseek`, the `deepseek` custom provider, and model `deepseek-v4-flash`.

DeepSeek configuration is passed as process-local Codex `-c` overrides. The app does not rewrite either provider's `config.toml`; set `DEEPSEEK_API_KEY` in the environment before launching the app, or save a key in the GUI. GUI-saved keys are stored in the operating-system keyring when available, with a user-private `0600` file fallback on minimal Linux installations. They are injected only into the DeepSeek process environment and are never stored in this repository or emitted in diagnostics. Clear removes the locally saved key; an existing environment variable remains available as a fallback.

Thread history records its provider and model. Resume always routes through the matching App Server process, so changing the picker cannot resume a Thread in the other provider's session space.

## Project workspace

The desktop workspace is organized as `Project → Provider → Thread → Turn → Item`. Project metadata and lightweight Thread indexes are stored in a local SQLite registry at `~/.local/share/mint-codex/workspace.sqlite3`; Codex conversation bodies remain owned by the App Server and are not copied into that registry.

The main window restores the last Project and Thread context, uses the Project's absolute path for `thread/start.cwd` and `thread/list.cwd`, and presents a Project tree with each project's indexed Threads and an inline New Thread button. Provider credentials are available from Settings, not the main workspace. Timeline events are converted into domain items before Qt rendering so user messages, agent messages, commands, file changes, tool calls, status, and errors remain distinct.

Runtime project selection has one authority: `WorkspaceState.active_project_id`. Sidebar selection, header text, Thread grouping, composer state, and App Server cwd are synchronized projections of that stable id. Project activation and cross-Project Thread resume use the same transition path; stale asynchronous history or Thread responses are discarded after a workspace switch.

## MVP-4 developer workspace

The right-side Developer Workspace is scoped to the authoritative active Project. It asynchronously observes real Git status and diff data, provides safe read-only file preview inside the Project root, and offers a Linux PTY terminal whose process cwd is fixed to that Project. Git is read-only in this layer; commit, push, reset, clean, and other Git write operations are intentionally not exposed.

Run the test suite with:

```bash
pytest -q
```
