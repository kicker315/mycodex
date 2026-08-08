from __future__ import annotations

import stat

from mint_codex.core.secrets import SecretStore


def test_saved_secret_survives_store_recreation_and_clear(tmp_path):
    path = tmp_path / "secrets.json"
    first = SecretStore(path=path, use_keyring=False)

    assert first.save("deepseek", "fixture-value") is True
    assert SecretStore(path=path, use_keyring=False).get("deepseek") == "fixture-value"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    assert first.delete("deepseek") is True
    assert SecretStore(path=path, use_keyring=False).get("deepseek") is None
