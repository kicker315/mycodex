from __future__ import annotations

import re

_SENSITIVE_VALUE = re.compile(
    r"(?i)(access[_-]?token|refresh[_-]?token|id[_-]?token|authorization|api[_-]?key)"
    r"(\s*[=:]\s*|\s+)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


def redact_sensitive(text: str) -> str:
    """Remove common credential shapes from external-process diagnostics."""

    text = _BEARER.sub("Bearer [REDACTED]", text)
    return _SENSITIVE_VALUE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
