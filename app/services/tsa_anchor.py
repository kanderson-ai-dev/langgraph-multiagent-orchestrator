"""Optional RFC 3161 timestamp anchoring for the audit anchor log.

The local ``audit_anchor.jsonl`` proves a job's ``audit_root`` existed in
this log; an RFC 3161 Time-Stamp Token (TST) from an external TSA proves
*when* it existed to anyone trusting that TSA — the owner can show the
root was sealed before any later DB rewrite is even possible.

Design:

- Disabled by default: ``AUDIT_ANCHOR_TSA_URL=""`` stamps nothing.
- The stamped message is ``bytes.fromhex(audit_root)`` — the raw
  32-byte digest — so verification recomputes ``sha256(root_bytes)``
  and compares it to the token's ``messageImprint``.
- Receipts live in a sidecar ``<anchor>.tsa.jsonl`` so the primary log
  stays append-only and untouched by retrofit stamping.
- Requested with ``certReq=true``: the token embeds the TSA signer
  certificate, which is what ``verify_receipt`` uses. Trust model is
  documented in ADR 0005: the token proves a TSA key signed this digest
  at ``genTime``; deciding *which* TSA keys are trusted is the
  deployment's job (the receipt records the cert fingerprint so a
  different TSA key is detectable).

``rfc3161-client`` is an optional dependency (``[project.optional-
dependencies] anchoring``); every public function degrades to a clear
error when it is not installed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.serialization import Encoding


def tsa_path_for(anchor_path: str | Path) -> str:
    """Sidecar receipt log living next to the anchor log."""
    return str(anchor_path) + ".tsa.jsonl"


def _default_roots() -> list[Any]:
    """Public CA roots from the certifi bundle (installed via httpx).

    Used to chain-verify a TSA's embedded certificate; a self-signed
    TSA certificate is added by the caller as its own root.
    """
    import certifi
    from cryptography import x509

    bundle = Path(certifi.where()).read_bytes()
    # A few bundled legacy roots carry non-positive serials — harmless
    # for trust-anchor use, and not worth a deprecation warning each call.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return list(x509.load_pem_x509_certificates(bundle))


def _tsp() -> Any:
    """Import the optional client lazily so the core never depends on it."""
    try:
        import rfc3161_client
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "rfc3161-client not installed — "
            "install the 'anchoring' extra to use TSA stamping"
        ) from exc
    return rfc3161_client


async def request_timestamp(
    tsa_url: str,
    audit_root: str,
    *,
    timeout: float = 10.0,
    verify: ssl.SSLContext | bool = True,
) -> dict[str, Any]:
    """Request a timestamp token for ``audit_root`` and verify it.

    Returns a JSON-serialisable receipt::

        {"tsa_url", "tsa_token" (DER, base64), "tsa_time" (genTime ISO),
         "tsa_cert_sha256", "message_sha256"}

    Raises on any transport, protocol or verification failure — the
    caller decides whether retrying is worthwhile.
    """
    tsp = _tsp()
    message = bytes.fromhex(audit_root)
    req = tsp.TimestampRequestBuilder(
        data=message,
        hash_algorithm=tsp.HashAlgorithm.SHA256,
        nonce=False,
        cert_req=True,
    ).build()
    async with httpx.AsyncClient(timeout=timeout, verify=verify) as client:
        resp = await client.post(
            tsa_url,
            content=req.as_bytes(),
            headers={
                "Content-Type": "application/timestamp-query",
                "Accept": "application/timestamp-reply",
            },
        )
        resp.raise_for_status()
    return receipt_from_token(resp.content, audit_root)


def receipt_from_token(token_der: bytes, audit_root: str) -> dict[str, Any]:
    """Verify a raw ``TimeStampResp`` DER and build the receipt dict."""
    tsp = _tsp()
    message = bytes.fromhex(audit_root)
    resp = tsp.decode_timestamp_response(token_der)
    status = getattr(resp, "status", None)
    granted = (tsp.PKIStatus.GRANTED, tsp.PKIStatus.GRANTED_WITH_MODS)
    if status is not None and int(status) not in (int(s) for s in granted):
        raise ValueError(f"TSA refused the request: {status}")
    certs = list(resp.signed_data.certificates or [])
    if not certs:
        raise ValueError("timestamp token carries no signer certificate")
    from cryptography import x509

    certs = [
        c if isinstance(c, x509.Certificate)
        else x509.load_der_x509_certificate(c)
        for c in certs
    ]
    signer = certs[0]
    builder = tsp.VerifierBuilder().tsa_certificate(signer)
    for c in _default_roots():
        builder = builder.add_root_certificate(c)
    if signer.issuer == signer.subject:
        builder = builder.add_root_certificate(signer)
    for c in certs[1:]:
        if c.subject != signer.subject:
            builder = builder.add_intermediate_certificate(c)
    if not builder.build().verify_message(resp, message):
        raise ValueError("timestamp token failed signature/imprint verify")
    tst = resp.tst_info
    gen_time = getattr(tst, "gen_time", None)
    return {
        "tsa_token": base64.b64encode(token_der).decode("ascii"),
        "tsa_time": (
            gen_time.isoformat() if gen_time is not None else None
        ),
        "tsa_cert_sha256": hashlib.sha256(
            signer.public_bytes(Encoding.DER)
        ).hexdigest(),
        "message_sha256": hashlib.sha256(message).hexdigest(),
    }


def append_tsa_receipt(
    anchor_path: str | Path,
    *,
    job_id: str,
    workspace_id: str,
    audit_root: str,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Append one receipt line to the sidecar log (fsync'd, append-only)."""
    entry = {
        "job_id": job_id,
        "workspace_id": workspace_id,
        "audit_root": audit_root,
        "stamped_at": datetime.now(UTC).isoformat(),
        **receipt,
    }
    path = Path(tsa_path_for(anchor_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return entry


def read_tsa_receipts(anchor_path: str | Path) -> list[dict[str, Any]]:
    """Read the sidecar back; missing/corrupt lines yield nothing."""
    path = Path(tsa_path_for(anchor_path))
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and "job_id" in row:
            out.append(row)
    return out


def verify_receipt(entry: dict[str, Any]) -> dict[str, Any]:
    """Offline re-verification of one stored receipt.

    Returns ``{"job_id", "audit_root", "valid", "detail"}`` — never
    raises on malformed entries (a corrupt receipt reports ``valid``:
    false with the reason).
    """
    job_id = entry.get("job_id")
    root = entry.get("audit_root")
    token = entry.get("tsa_token")
    if not isinstance(root, str) or not isinstance(token, str):
        return {
            "job_id": job_id,
            "audit_root": root,
            "valid": False,
            "detail": "missing audit_root or tsa_token",
        }
    try:
        receipt = receipt_from_token(base64.b64decode(token), root)
    except Exception as exc:
        return {
            "job_id": job_id,
            "audit_root": root,
            "valid": False,
            "detail": str(exc),
        }
    return {
        "job_id": job_id,
        "audit_root": root,
        "valid": True,
        "detail": receipt.get("tsa_time"),
    }


async def stamp_with_retries(
    tsa_url: str,
    anchor_path: str | Path,
    *,
    job_id: str,
    workspace_id: str,
    audit_root: str,
    attempts: int = 3,
    base_delay: float = 1.0,
) -> dict[str, Any]:
    """Stamp ``audit_root`` at ``tsa_url`` with bounded backoff retries.

    Only the last error propagates — the caller wraps this in a
    fire-and-forget task so a TSA outage can never fail a sealed job.
    """
    import asyncio

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            receipt = await request_timestamp(tsa_url, audit_root)
            receipt["tsa_url"] = tsa_url
            return append_tsa_receipt(
                anchor_path,
                job_id=job_id,
                workspace_id=workspace_id,
                audit_root=audit_root,
                receipt=receipt,
            )
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(base_delay * 2**attempt)
    assert last is not None
    raise last
