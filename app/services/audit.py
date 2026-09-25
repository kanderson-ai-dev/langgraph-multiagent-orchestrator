"""Tamper-evident audit trail — a SHA-256 hash chain over the transcript.

Every transcript message is folded into a running digest:
``hash_i = sha256(canonical(msg_i) | hash_{i-1})``. The final digest
(``audit_root``) is delivered with the report; anyone can recompute the chain
from the stored transcript and any alteration — a deleted verdict, an edited
dispatch — invalidates the root.

For B2B deliverables this turns "trust our process" into "verify our process".
"""

import hashlib
import json

from pydantic import BaseModel

from app.graph.state import AgentMessage

GENESIS = "0" * 64


class AuditEntry(BaseModel):
    """One link of the chain — a transcript message plus its hashes."""

    index: int
    sender: str
    kind: str
    prev_hash: str
    hash: str


def _canonical(msg: AgentMessage) -> str:
    """Deterministic serialization of the message fields that matter."""
    return json.dumps(
        {
            "sender": msg.sender,
            "recipient": msg.recipient,
            "kind": msg.kind,
            "content": msg.content,
            "round": msg.round,
            "created_at": msg.created_at.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def chain_transcript(messages: list[AgentMessage]) -> list[AuditEntry]:
    """Build the hash chain over ``messages`` (empty list → empty chain)."""
    entries: list[AuditEntry] = []
    prev = GENESIS
    for i, m in enumerate(messages):
        digest = hashlib.sha256(f"{prev}|{_canonical(m)}".encode()).hexdigest()
        entries.append(
            AuditEntry(
                index=i, sender=m.sender, kind=m.kind,
                prev_hash=prev, hash=digest,
            )
        )
        prev = digest
    return entries


def audit_root(messages: list[AgentMessage]) -> str:
    """The chain tip — the fingerprint shipped with the report."""
    entries = chain_transcript(messages)
    return entries[-1].hash if entries else GENESIS


def verify_chain(messages: list[AgentMessage], expected_root: str) -> bool:
    """Recompute the chain and compare — constant-shaped, O(n)."""
    import hmac

    return hmac.compare_digest(audit_root(messages), expected_root)
