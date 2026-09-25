"""Logging: sensitive field redaction."""

import logging

from app.core.logging import redact_sensitive_fields


def test_redacts_sensitive_keys() -> None:
    event = {
        "msg": "request",
        "openai_api_key": "sk-secret",
        "SEARCH_API_KEY": "tvly-secret",
        "access_token": "tok",
        "admin_password": "pw",
        "authorization": "Bearer x",
        "db_secret": "s3cr3t",
        "safe_field": "visible",
        "keynote": "not-a-secret-suffix-match-ok",  # does not end in _key
    }
    out = redact_sensitive_fields(logging.getLogger("t"), "info", event)
    assert out["openai_api_key"] == "***REDACTED***"
    assert out["SEARCH_API_KEY"] == "***REDACTED***"
    assert out["access_token"] == "***REDACTED***"
    assert out["admin_password"] == "***REDACTED***"
    assert out["authorization"] == "***REDACTED***"
    assert out["db_secret"] == "***REDACTED***"
    assert out["safe_field"] == "visible"
    assert out["keynote"] == "not-a-secret-suffix-match-ok"
