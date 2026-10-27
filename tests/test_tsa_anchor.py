"""RFC 3161 timestamp anchoring (optional 'anchoring' extra).

Tests run against a local fake TSA: it parses the client's real
``TimeStampReq`` DER, builds a ``TSTInfo`` over the request's
``messageImprint``, signs it into a PKCS7 ``SignedData`` with a test key
(eContentType patched to ``id-ct-TSTInfo``), and wraps it in a
``TimeStampResp`` — so the client path under test is the genuine
request→response→verify flow, not a fixture.
"""

import base64
import hashlib
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi.testclient import TestClient

from app.services.tsa_anchor import (
    append_tsa_receipt,
    read_tsa_receipts,
    request_timestamp,
    stamp_with_retries,
    tsa_path_for,
    verify_receipt,
)
from tests.test_audit_signing import _app, _done_job

TSA_URL = "https://tsa.test/timestamp"

_SHA256_OID = bytes.fromhex("608648016503040201")
_TSTINFO_OID = bytes.fromhex("2a864886f70d0109100104")
_RSA_OID = bytes.fromhex("2a864886f70d010101")


def _tlv(tag: int, content: bytes) -> bytes:
    n = len(content)
    if n < 128:
        length = bytes([n])
    elif n < 256:
        length = bytes([0x81, n])
    else:
        length = bytes([0x82, n >> 8, n & 0xFF])
    return bytes([tag]) + length + content


def _seq(content: bytes) -> bytes:
    return _tlv(0x30, content)


def _int(v: int) -> bytes:
    return _tlv(0x02, v.to_bytes((v.bit_length() + 7) // 8 or 1, "big"))


def _oid(body: bytes) -> bytes:
    return _tlv(0x06, body)


def _octet(b: bytes) -> bytes:
    return _tlv(0x04, b)


def _gentime(s: str) -> bytes:
    return _tlv(0x18, s.encode())


def _children(der: bytes) -> list[bytes]:
    """Raw TLV bytes of each child of a SEQUENCE/SET."""
    assert der[0] in (0x30, 0x31)
    i = 1
    i += 1 + (der[i] & 0x7F) if der[i] & 0x80 else 1
    out: list[bytes] = []
    while i < len(der):
        start = i
        i += 1
        lb = der[i]
        i += 1
        if lb & 0x80:
            nlen = lb & 0x7F
            n = int.from_bytes(der[i : i + nlen], "big")
            i += nlen
        else:
            n = lb
        i += n
        out.append(der[start:i])
    return out


def _content(der: bytes) -> bytes:
    i = 1
    lb = der[i]
    i += 1
    if lb & 0x80:
        nlen = lb & 0x7F
        i += nlen
        n = int.from_bytes(der[i - nlen : i], "big")
    else:
        n = lb
    return der[i : i + n]


def _request_digest(req_der: bytes) -> bytes:
    """messageImprint digest out of a real TimeStampReq."""
    mi = _children(req_der)[1]  # SEQ { AlgorithmIdentifier, OCTET STRING }
    return _content(_children(mi)[1])


def _fake_tsa_cert() -> tuple[Any, x509.Certificate]:
    key = rsa.generate_private_key(65537, 2048)
    now = datetime.now(UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test TSA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(7)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]),
            critical=True,
        )
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _fake_tsa_response(
    req_der: bytes, key: Any, cert: x509.Certificate
) -> bytes:
    """Build a real TimeStampResp DER over the request's imprint."""
    digest = _request_digest(req_der)
    gen = datetime.now(UTC).strftime("%Y%m%d%H%M%SZ")
    mi = _seq(_seq(_oid(_SHA256_OID) + _tlv(0x05, b"")) + _octet(digest))
    tstinfo = _seq(
        _int(1) + _oid(_TSTINFO_OID) + mi + _int(99) + _gentime(gen)
    )
    content_info = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(tstinfo)
        .add_signer(cert, key, hashes.SHA256())
        .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])
    )
    # Patch eContentType OID data -> id-ct-TSTInfo (RFC 3161 §2.4.2)
    ci_children = _children(content_info)
    sd_der = _content(ci_children[1])
    sd_parts = _children(sd_der)
    encap_children = _children(sd_parts[2])
    encap_new = _seq(_oid(_TSTINFO_OID) + encap_children[1])
    sd_new = _seq(
        b"".join([sd_parts[0], sd_parts[1], encap_new] + sd_parts[3:])
    )
    token = _seq(ci_children[0] + _tlv(0xA0, sd_new))
    return _seq(_seq(_int(0)) + token)  # status=granted + token


@pytest.fixture()
def tsa() -> Any:
    key, cert = _fake_tsa_cert()
    return key, cert


def _route(key: Any, cert: x509.Certificate) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_fake_tsa_response(request.content, key, cert),
            headers={"Content-Type": "application/timestamp-reply"},
        )

    return respx.post(TSA_URL).mock(side_effect=handler)


_ROOT = "ab" * 32


async def test_request_timestamp_end_to_end(tsa: Any) -> None:
    key, cert = tsa
    with respx.mock:
        _route(key, cert)
        receipt = await request_timestamp(TSA_URL, _ROOT)
    assert receipt["message_sha256"] == hashlib.sha256(
        bytes.fromhex(_ROOT)
    ).hexdigest()
    assert receipt["tsa_time"] is not None
    assert receipt["tsa_cert_sha256"] == hashlib.sha256(
        cert.public_bytes(serialization.Encoding.DER)
    ).hexdigest()
    # The stored token must re-verify offline against the same root.
    assert verify_receipt({"job_id": "j", "audit_root": _ROOT,
                           "tsa_token": receipt["tsa_token"]})["valid"]


def test_receipt_roundtrip_and_tamper(tmp_path: Any, tsa: Any) -> None:
    key, cert = tsa
    with respx.mock:
        _route(key, cert)
        receipt = __import__("asyncio").run(
            request_timestamp(TSA_URL, _ROOT)
        )
    anchor = str(tmp_path / "anchor.jsonl")
    append_tsa_receipt(
        anchor, job_id="j1", workspace_id="w",
        audit_root=_ROOT, receipt=receipt,
    )
    rows = read_tsa_receipts(anchor)
    assert len(rows) == 1 and rows[0]["job_id"] == "j1"
    assert verify_receipt(rows[0])["valid"] is True

    tampered = {**rows[0], "audit_root": "cd" * 32}
    outcome = verify_receipt(tampered)
    assert outcome["valid"] is False


async def test_stamp_with_retries_eventually_writes(
    tmp_path: Any, tsa: Any
) -> None:
    key, cert = tsa
    calls = 0
    anchor = str(tmp_path / "anchor.jsonl")

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503)
        return httpx.Response(
            200,
            content=_fake_tsa_response(request.content, key, cert),
            headers={"Content-Type": "application/timestamp-reply"},
        )

    with respx.mock:
        respx.post(TSA_URL).mock(side_effect=flaky)
        entry = await stamp_with_retries(
            TSA_URL, anchor,
            job_id="j1", workspace_id="w", audit_root=_ROOT,
            base_delay=0.01,
        )
    assert calls == 3
    assert entry["job_id"] == "j1"
    assert len(read_tsa_receipts(anchor)) == 1


async def test_stamp_with_retries_gives_up(tmp_path: Any) -> None:
    anchor = str(tmp_path / "anchor.jsonl")
    with respx.mock:
        respx.post(TSA_URL).mock(return_value=httpx.Response(503))
        with pytest.raises(httpx.HTTPStatusError):
            await stamp_with_retries(
                TSA_URL, anchor,
                job_id="j1", workspace_id="w", audit_root=_ROOT,
                attempts=2, base_delay=0.01,
            )
    assert read_tsa_receipts(anchor) == []


def test_sidecar_path(tmp_path: Any) -> None:
    assert tsa_path_for(tmp_path / "a.jsonl").endswith("a.jsonl.tsa.jsonl")


def test_sealed_job_schedules_tsa_stamp(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """With ``audit_anchor_tsa_url`` set, sealing a job schedules the
    bounded stamping task — verified deterministically via an event the
    patched coroutine sets inside the app loop."""
    stamped = threading.Event()

    async def fake_stamp(*args: Any, **kwargs: Any) -> dict[str, Any]:
        stamped.set()
        return {}

    monkeypatch.setattr(
        "app.services.tsa_anchor.stamp_with_retries", fake_stamp
    )
    with TestClient(_app(tmp_path, tsa_url=TSA_URL)) as client:
        _done_job(client)
    assert stamped.wait(timeout=10)


def test_disabled_by_default_no_task(
    tmp_path: Any, monkeypatch: Any
) -> None:
    spy = AsyncMock(return_value={})
    monkeypatch.setattr(
        "app.services.tsa_anchor.stamp_with_retries", spy
    )
    with TestClient(_app(tmp_path)) as client:
        _done_job(client)
    spy.assert_not_called()


def test_verify_receipt_malformed() -> None:
    assert verify_receipt({"job_id": "j"})["valid"] is False
    bad = verify_receipt(
        {
            "job_id": "j",
            "audit_root": _ROOT,
            "tsa_token": base64.b64encode(b"garbage").decode(),
        }
    )
    assert bad["valid"] is False
