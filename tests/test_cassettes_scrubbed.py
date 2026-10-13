"""Cassette hygiene — no secret may survive in a recorded interaction.

Fails CI if a cassette contains an API key, bearer token, or an
``Authorization``-family request header. Recorded web-page bodies can
legitimately contain the words "bearer"/"authorization", so the checks
target *secret-shaped* values, not mere words — plus the verbatim value
of any key actually configured in the environment.
"""

import os
import re
from pathlib import Path

import yaml

CASSETTE_DIR = Path(__file__).parent / "cassettes"

# Secret-shaped patterns — provider prefixes and long bearer/api tokens.
_FORBIDDEN_PATTERNS = [
    re.compile(r"sk-proj-"),
    re.compile(r"sk-ant-"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"tvly-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}"),
    re.compile(r"api[_-]?key=[A-Za-z0-9]{16,}"),
]

_FORBIDDEN_HEADERS = {
    "authorization",
    "x-api-key",
    "api-key",
    "openai-organization",
    "openai-organization-id",
    "cookie",
    "set-cookie",
}


def _iter_header_names(data: object) -> list[str]:
    """Collect every request *and* response header name in a cassette."""
    names: list[str] = []
    interactions = (data or {}).get("interactions", []) if isinstance(data, dict) else []
    for interaction in interactions:
        if not isinstance(interaction, dict):
            continue
        for side in ("request", "response"):
            headers = interaction.get(side, {}).get("headers", {})
            names.extend(str(h).lower() for h in headers)
    return names


def test_no_cassette_contains_secret_values() -> None:
    # HTTP cassettes (.yaml) plus the recorded-search sidecars (.json).
    cassettes = [
        *CASSETTE_DIR.glob("*.yaml"), *CASSETTE_DIR.glob("*.json")
    ]
    assert cassettes, "no cassettes recorded — nothing to verify"
    env_secrets = [
        v for k in ("OPENAI_API_KEY", "SEARCH_API_KEY", "TAVILY_API_KEY")
        if (v := os.environ.get(k))
    ]
    for path in cassettes:
        text = path.read_text(encoding="utf-8")
        for pattern in _FORBIDDEN_PATTERNS:
            assert not pattern.search(text), (
                f"{path.name}: secret-shaped value matches {pattern.pattern}"
            )
        for secret in env_secrets:
            assert secret not in text, (
                f"{path.name}: verbatim environment secret present"
            )


def test_no_cassette_records_auth_headers() -> None:
    for path in CASSETTE_DIR.glob("*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        leaked = set(_iter_header_names(data)) & _FORBIDDEN_HEADERS
        assert not leaked, f"{path.name}: auth headers recorded: {leaked}"
