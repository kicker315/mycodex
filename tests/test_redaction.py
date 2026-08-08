from mint_codex.core.redaction import redact_sensitive


def test_sensitive_credentials_are_redacted():
    text = "Authorization: Bearer secret.token access_token=abc123 api-key: xyz789 harmless=value"
    safe = redact_sensitive(text)
    assert "secret.token" not in safe
    assert "abc123" not in safe
    assert "xyz789" not in safe
    assert safe.count("[REDACTED]") >= 3
    assert "harmless=value" in safe


def test_explicit_process_secret_is_redacted():
    assert redact_sensitive("provider diagnostics: raw-secret", ["raw-secret"]) == "provider diagnostics: [REDACTED]"
