"""Tamper-evident audit trail — a SHA-256 hash chain over the transcript.

Every transcript message is folded into a running digest:
``hash_i = sha256(canonical(msg_i) | hash_{i-1})``. The final digest
(``audit_root``) is delivered with the report; anyone can recompute the chain
from the stored transcript and any alteration — a deleted verdict, an edited
dispatch — invalidates the root.

For B2B deliverables this turns "trust our process" into "verify our process".
"""

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from app.graph.state import AgentMessage

GENESIS = "0" * 64


class AuditEntry(BaseModel):
    """One link of the chain — a transcript message plus its hashes."""

    index: int
    sender: str
    kind: str
    prompt_version: int | None = None
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
            "prompt_version": msg.prompt_version,
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
                prompt_version=m.prompt_version,
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
    return hmac.compare_digest(audit_root(messages), expected_root)


def _canonical_scope(root: str, job_id: str, workspace_id: str) -> bytes:
    return f"{workspace_id}|{job_id}|{root}".encode()


def key_id_for(key: str) -> str:
    """Short non-secret key fingerprint — first 8 hex of ``sha256(key)``.

    Stored beside each signature so a *keyring* (current + retired keys)
    can rotate without invalidating old signatures: only the id travels,
    never the key material.
    """
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def signing_keyring(settings: Any) -> dict[str, str]:
    """Build ``{key_id: key}`` from ``AUDIT_SIGNING_KEY`` (current) plus
    comma-separated ``AUDIT_SIGNING_PREVIOUS_KEYS`` (verification only).
    The current key wins on an id collision."""
    ring: dict[str, str] = {}
    cur = getattr(settings, "audit_signing_key", None)
    if cur is not None:
        key = cur.get_secret_value()
        ring[key_id_for(key)] = key
    prev = getattr(settings, "audit_signing_previous_keys", None)
    if prev is not None:
        for key in prev.get_secret_value().split(","):
            key = key.strip()
            if key:
                ring.setdefault(key_id_for(key), key)
    return ring


def signature_key_id(signature: str | None) -> str | None:
    """The ``key_id`` stored in a ``key_id:hmac`` signature, if any."""
    if signature and ":" in signature:
        return signature.split(":", 1)[0]
    return None


def sign_root(
    key: str, root: str, *, job_id: str, workspace_id: str
) -> str:
    """``key_id:HMAC-SHA256`` over ``workspace|job|root`` — proves the
    chain tip was sealed by this deployment, not recomputed by a DB
    writer. The key-id prefix lets verifiers pick the right ring member
    after a rotation."""
    mac = hmac.new(
        key.encode(), _canonical_scope(root, job_id, workspace_id),
        hashlib.sha256,
    ).hexdigest()
    return f"{key_id_for(key)}:{mac}"


def verify_signature(
    keyring: Mapping[str, str],
    signature: str,
    root: str,
    *,
    job_id: str,
    workspace_id: str,
) -> bool:
    """Constant-time check of a stored audit-root signature.

    ``key_id:hmac`` signatures verify against the named ring key — an
    unknown id (a fully retired key) returns ``False``, not an error.
    Legacy signatures carry no prefix and are tried against every key.
    """
    kid, sep, sig = signature.partition(":")
    if sep:
        key = keyring.get(kid)
        if key is None:
            return False
        keys = [key]
    else:
        sig = signature
        keys = list(keyring.values())
    scope = _canonical_scope(root, job_id, workspace_id)
    for key in keys:
        expected = hmac.new(key.encode(), scope, hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected):
            return True
    return False
