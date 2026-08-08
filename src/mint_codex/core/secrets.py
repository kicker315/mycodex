from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class SecretStore:
    """Persist provider credentials without putting them in Codex config files.

    The system keyring is preferred.  A private, mode-0600 file is used only
    when no usable keyring backend is available, which keeps the feature
    usable on minimal Linux installations without exposing the value to the
    project directory or process logs.
    """

    service_name = "mint-codex-desktop"

    def __init__(self, path: Path | None = None, use_keyring: bool = True):
        self.path = (path or Path.home() / ".config" / "mint-codex" / "secrets.json").expanduser()
        self._keyring: Any = self._load_keyring() if use_keyring else None
        self._storage = "OS keyring" if self._keyring is not None else "private file"

    @property
    def storage_description(self) -> str:
        return self._storage

    def get(self, name: str) -> str | None:
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(self.service_name, name)
                if isinstance(value, str) and value:
                    return value
            except Exception:
                self._disable_keyring()
        value = self._read_file().get(name)
        return value if isinstance(value, str) and value else None

    def save(self, name: str, value: str) -> bool:
        if not value:
            return self.delete(name)
        if self._keyring is not None:
            try:
                self._keyring.set_password(self.service_name, name, value)
                self._remove_from_file(name)
                return True
            except Exception:
                self._disable_keyring()
        values = self._read_file()
        values[name] = value
        return self._write_file(values)

    def delete(self, name: str) -> bool:
        if self._keyring is not None:
            try:
                self._keyring.delete_password(self.service_name, name)
            except Exception:
                # A missing key is already the requested state. Other
                # failures fall back to removing the private-file copy.
                self._disable_keyring()
        values = self._read_file()
        if name not in values:
            return True
        values.pop(name, None)
        return self._write_file(values)

    @staticmethod
    def _load_keyring() -> Any:
        try:
            import keyring

            return keyring
        except ImportError:
            return None

    def _disable_keyring(self) -> None:
        self._keyring = None
        self._storage = "private file"

    def _read_file(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            mode = self.path.stat().st_mode & 0o777
            if mode & 0o077:
                os.chmod(self.path, 0o600)
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            return {key: value for key, value in data.items() if isinstance(key, str) and isinstance(value, str)}
        except (OSError, ValueError, AttributeError):
            log.warning("Ignoring unreadable local credential store")
            return {}

    def _write_file(self, values: dict[str, str]) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
            fd, temporary_name = tempfile.mkstemp(prefix="secrets.", dir=self.path.parent)
            try:
                os.chmod(temporary_name, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(values, handle, ensure_ascii=True, separators=(",", ":"))
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, self.path)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
                raise
            os.chmod(self.path, 0o600)
            return True
        except OSError:
            log.warning("Could not persist local credentials")
            return False

    def _remove_from_file(self, name: str) -> None:
        values = self._read_file()
        if name in values:
            values.pop(name, None)
            self._write_file(values)
