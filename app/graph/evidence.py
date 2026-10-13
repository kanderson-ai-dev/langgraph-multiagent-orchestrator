"""Evidence sealing — content addressing for fetched sources.

Every scraped/parsed source is hashed once at gather time
(``sha256(url | content_type | sanitized_text)``). The digest preimage
deliberately excludes ``fetched_at``: the timestamp lives in the manifest
item for auditing, but keeping it out of the hash means the same fetched
content always seals to the same manifest root — reproducibility is a
property the evaluation suite asserts.

The manifest root is sealed into the audit chain the moment the
Supervisor routes to the Writer: from that point any tampering with
stored evidence invalidates both verification and the sealed root.
"""

import hashlib
from typing import Any

from app.graph.state import EvidenceItem


def evidence_sha(url: str, content_type: str, sanitized_text: str) -> str:
    """Content address of one evidence item (deterministic, no timestamp)."""
    return hashlib.sha256(
        f"{url}|{content_type}|{sanitized_text}".encode()
    ).hexdigest()


def build_evidence_manifest(
    evidence: list[EvidenceItem],
) -> dict[str, Any]:
    """Sealed manifest over the gathered evidence, or an empty shape.

    Items are sorted by evidence_id so the root depends only on the
    content set, never on collection order. ``fetched_at`` is serialized
    per item but is NOT part of either digest (see module docstring).
    """
    items = sorted(
        (
            {
                "evidence_id": e.evidence_id,
                "url": e.url,
                "title": e.title,
                "sha256": e.sha256
                or evidence_sha(e.url, e.content_type, e.text),
                "content_type": e.content_type,
                "fetched_at": e.fetched_at.isoformat(),
            }
            for e in evidence
        ),
        key=lambda i: i["evidence_id"],
    )
    root = hashlib.sha256(
        "|".join(i["sha256"] for i in items).encode()
    ).hexdigest()
    return {"root": root, "count": len(items), "items": items}


def manifest_root(evidence: list[EvidenceItem]) -> str:
    return str(build_evidence_manifest(evidence)["root"])
