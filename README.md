# Mint Codex Desktop

A small native Linux/PySide6 client for the installed official `codex app-server`.
It does not implement an agent loop or call OpenAI APIs directly.

Run with:

```bash
python -m mint_codex.main
```

Set `QT_QPA_PLATFORM=offscreen` for a headless GUI smoke test.
