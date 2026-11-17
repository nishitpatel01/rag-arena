"""v1.6.0 Step 1 TDD: IAP JWT ES256 auth, public session cookie isolation, /ingest/jobs gate, Origin & CSP headers."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import time
from typing import Any
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
import pytest
from starlette.testclient import TestClient

from app.core.config import ArenaSettings, get_settings
from app.ingestion.jobs import IngestBackends, IngestJob, IngestJobRunner
from app.ingestion.upload_bucket import BucketState
from app.main import create_app
from app.recording.redaction import assert_redacted, redact_value, redaction_tokens
from fakes import live_settings
from test_uploads_api import FakeManager, FakeServices, FakeStore, _RowsLoader

TEST_AUDIENCE = "/projects/76543/locations/us-central1/services/rag-arena"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_es256_keypair(kid: str = "iap-key-1") -> tuple[ec.EllipticCurvePrivateKey, dict[str, str]]:
    """Generate an ephemeral P-256 keypair and PEM public key map matching Google IAP's public_key endpoint."""
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return priv, {kid: pem}


def sign_iap_jwt(
    priv: ec.EllipticCurvePrivateKey,
    *,
    email: str = "accounts.google.com:alice@example.com",
    sub: str = "accounts.google.com:1001",
    aud: str = TEST_AUDIENCE,
    iss: str = "https://cloud.google.com/iap",
    kid: str = "iap-key-1",
    iat_offset: int = -10,
    exp_offset: int = 300,
) -> str:
    """Create a real RFC 7518 ES256 JWT (raw R||S 64-byte signature) as emitted by Cloud Run IAP."""
    now = int(time.time())
    header = {"alg": "ES256", "typ": "JWT", "kid": kid}
    payload = {"iss": iss, "aud": aud, "sub": sub, "email": email, "iat": now + iat_offset, "exp": now + exp_offset}
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}".encode("ascii")
    der_sig = priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input.decode('ascii')}.{_b64url(raw_sig)}"


def _iap_app(certs: dict[str, str], **overrides: Any) -> tuple[TestClient, FakeStore]:
    from app.core import auth as auth_mod

    settings = live_settings(
        access_mode="iap",
        iap_jwt_audience=TEST_AUDIENCE,
        **overrides,
    )
    store = FakeStore()
    app = create_app(
        settings=settings,
        query_run_writer=object(),
        ingest_backend_factory=lambda s: IngestBackends(None, _RowsLoader()),  # type: ignore[arg-type]
    )
    app.state.upload_services = FakeServices(FakeManager(BucketState.OK), store)
    app.state.iap_certs_override = certs
    assert hasattr(auth_mod, "verify_iap_jwt")
    return TestClient(app), store


def test_cloud_run_defaults_fail_closed_to_iap_and_require_audience() -> None:
    base_env = {"DEMO_MODE": "live", "GCP_PROJECT_ID": "test-project", "K_SERVICE": "rag-arena"}
    with pytest.raises(ValueError, match="IAP_JWT_AUDIENCE"):
        get_settings(base_env)

    configured = get_settings({**base_env, "IAP_JWT_AUDIENCE": TEST_AUDIENCE})
    assert configured.access_mode == "iap" and configured.iap_jwt_audience == TEST_AUDIENCE

    explicit_pub = get_settings({**base_env, "ACCESS_MODE": "public"})
    assert explicit_pub.access_mode == "public"

    # Replay on Cloud Run does not require IAP_JWT_AUDIENCE
    replay_cr = get_settings({"DEMO_MODE": "replay", "K_SERVICE": "rag-arena-replay"})
    assert replay_cr.demo_mode == "replay"


def test_iap_jwt_redacted_by_recording_redaction_tokens() -> None:
    settings = live_settings(access_mode="iap", iap_jwt_audience=TEST_AUDIENCE)
    tokens = redaction_tokens(settings)
    scrubbed = redact_value({"aud": TEST_AUDIENCE}, tokens)
    assert TEST_AUDIENCE not in json.dumps(scrubbed)
    assert_redacted(scrubbed, tokens)


def test_iap_mode_verifies_es256_jwt_and_rejects_header_spoofing_and_bad_claims() -> None:
    priv, certs = make_es256_keypair()
    attacker_priv, _ = make_es256_keypair(kid="iap-key-1")
    client, _store = _iap_app(certs)

    # 1. Missing JWT + spoofed X-Goog-Authenticated-User-Email -> 401
    spoof = client.post(
        "/api/v1/uploads",
        headers={"X-Goog-Authenticated-User-Email": "accounts.google.com:alice@example.com"},
        json={},
    )
    assert spoof.status_code == 401

    # 2. Unsigned alg=none token -> 401
    none_tok = f"{_b64url(b'{\"alg\":\"none\",\"typ\":\"JWT\"}')}.{_b64url(json.dumps({'iss': 'https://cloud.google.com/iap', 'aud': TEST_AUDIENCE, 'email': 'a@b.c', 'exp': int(time.time()) + 300}).encode())}."
    assert client.get("/api/v1/uploads", headers={"X-Goog-IAP-JWT-Assertion": none_tok}).status_code == 401

    # 3. Wrong signature, expired, wrong audience, wrong issuer -> all 401
    for bad_jwt in (
        sign_iap_jwt(attacker_priv),
        sign_iap_jwt(priv, exp_offset=-60, iat_offset=-120),
        sign_iap_jwt(priv, aud="/projects/99999/locations/us-central1/services/other"),
        sign_iap_jwt(priv, iss="https://accounts.google.com"),
    ):
        resp = client.get("/api/v1/uploads", headers={"X-Goog-IAP-JWT-Assertion": bad_jwt})
        assert resp.status_code == 401, resp.text

    # 4. Valid signed JWT succeeds, and ignores conflicting spoofed email header
    alice_jwt = sign_iap_jwt(priv, email="accounts.google.com:alice@example.com")
    bob_jwt = sign_iap_jwt(priv, email="accounts.google.com:bob@example.com")
    created = client.post(
        "/api/v1/uploads",
        headers={
            "X-Goog-IAP-JWT-Assertion": alice_jwt,
            "X-Goog-Authenticated-User-Email": "accounts.google.com:mallory@example.com",
        },
        json={},
    )
    assert created.status_code == 201, created.text
    ws_id = created.json()["workspace"]["id"]

    # Bob cannot read Alice's workspace even if Bob spoofs Alice's email header
    bob_try = client.get(
        f"/api/v1/uploads/{ws_id}",
        headers={
            "X-Goog-IAP-JWT-Assertion": bob_jwt,
            "X-Goog-Authenticated-User-Email": "accounts.google.com:alice@example.com",
        },
    )
    assert bob_try.status_code == 404
    assert client.get(f"/api/v1/uploads/{ws_id}", headers={"X-Goog-IAP-JWT-Assertion": alice_jwt}).status_code == 200


def test_public_mode_isolates_callers_via_httponly_cookie_and_ignores_email_header() -> None:
    settings = live_settings(access_mode="public")
    store = FakeStore()
    app = create_app(
        settings=settings,
        query_run_writer=object(),
        ingest_backend_factory=lambda s: IngestBackends(None, _RowsLoader()),  # type: ignore[arg-type]
    )
    app.state.upload_services = FakeServices(FakeManager(BucketState.OK), store)

    client_a = TestClient(app)
    client_b = TestClient(app)

    res_a = client_a.post(
        "/api/v1/uploads",
        headers={"X-Goog-Authenticated-User-Email": "accounts.google.com:shared@example.com"},
        json={},
    )
    assert res_a.status_code == 201
    set_cookie = res_a.headers.get("set-cookie", "")
    assert "httponly" in set_cookie.lower() and "samesite=lax" in set_cookie.lower()
    ws_a = res_a.json()["workspace"]["id"]

    # Second browser with the EXACT SAME spoofed X-Goog-Authenticated-User-Email header gets its own session -> 404!
    res_b_get = client_b.get(
        f"/api/v1/uploads/{ws_a}",
        headers={"X-Goog-Authenticated-User-Email": "accounts.google.com:shared@example.com"},
    )
    assert res_b_get.status_code == 404
    assert client_a.get(f"/api/v1/uploads/{ws_a}").status_code == 200


def test_ingest_jobs_endpoint_enforces_mode_live_ingest_iap_and_concurrency() -> None:
    # 1. Cloud Run Replay -> 404
    replay_client = TestClient(create_app(settings=ArenaSettings(demo_mode="replay")))
    with patch.dict(os.environ, {"K_SERVICE": "rag-arena-replay"}):
        assert replay_client.post("/api/v1/ingest/jobs", json={"level": "level1_naive"}).status_code == 404

    # 2. LIVE_INGEST=false -> 403
    ro_client = TestClient(create_app(settings=live_settings(live_ingest=False)))
    assert ro_client.post("/api/v1/ingest/jobs", json={"level": "level1_naive"}).status_code == 403

    # 3. IAP mode without valid JWT -> 401
    priv, certs = make_es256_keypair()
    iap_client, _ = _iap_app(certs)
    assert iap_client.post("/api/v1/ingest/jobs", json={"level": "level1_naive"}).status_code == 401

    # 4. Active concurrency cap (MAX_CONCURRENT_INGEST_JOBS = 2) -> 429
    runner: IngestJobRunner = iap_client.app.state.ingest_runner
    runner._jobs["ing_busy01"] = IngestJob(job_id="ing_busy01", level="level1_naive", tier_profile="naive", status="running")
    runner._jobs["ing_busy02"] = IngestJob(job_id="ing_busy02", level="level2_hybrid", tier_profile="hybrid", status="running")
    valid_jwt = sign_iap_jwt(priv)
    capped = iap_client.post(
        "/api/v1/ingest/jobs",
        headers={"X-Goog-IAP-JWT-Assertion": valid_jwt},
        json={"level": "level1_naive"},
    )
    assert capped.status_code == 429 and "concurrent" in capped.json()["error"].lower()


def test_origin_validation_blocks_host_header_spoofing_and_honors_allowlist() -> None:
    priv, certs = make_es256_keypair()
    jwt_hdr = {"X-Goog-IAP-JWT-Assertion": sign_iap_jwt(priv)}
    client, store = _iap_app(certs)
    ws = client.post("/api/v1/uploads", headers=jwt_hdr, json={}).json()["workspace"]["id"]

    # Localhost allowed off Cloud Run, but Host-header spoofing to an external domain is REJECTED
    spoof_local = client.post(
        f"/api/v1/uploads/{ws}/files",
        headers={**jwt_hdr, "Host": "evil.example.com", "Origin": "https://evil.example.com"},
        json={"filename": "a.pdf", "size_bytes": 10},
    )
    assert spoof_local.status_code == 400

    # On Cloud Run without ALLOWED_UPLOAD_ORIGINS: strict https://*.run.app matching Host is allowed; custom Host spoof fails
    with patch.dict(os.environ, {"K_SERVICE": "rag-arena"}):
        spoof_cr = client.post(
            f"/api/v1/uploads/{ws}/files",
            headers={**jwt_hdr, "Host": "evil.example.com", "Origin": "https://evil.example.com"},
            json={"filename": "b.pdf", "size_bytes": 10},
        )
        assert spoof_cr.status_code == 400

        ok_cr = client.post(
            f"/api/v1/uploads/{ws}/files",
            headers={**jwt_hdr, "Host": "rag-arena-xxxx-uc.a.run.app", "Origin": "https://rag-arena-xxxx-uc.a.run.app"},
            json={"filename": "c.pdf", "size_bytes": 10},
        )
        assert ok_cr.status_code == 201 and store.sessions[-1][3] == "https://rag-arena-xxxx-uc.a.run.app"

    # Explicit ALLOWED_UPLOAD_ORIGINS permits custom enterprise domain and blocks everything else
    custom_client, custom_store = _iap_app(certs, allowed_upload_origins=("https://rag.acme.example",))
    ws2 = custom_client.post("/api/v1/uploads", headers=jwt_hdr, json={}).json()["workspace"]["id"]
    with patch.dict(os.environ, {"K_SERVICE": "rag-arena"}):
        ok_custom = custom_client.post(
            f"/api/v1/uploads/{ws2}/files",
            headers={**jwt_hdr, "Host": "rag.acme.example", "Origin": "https://rag.acme.example"},
            json={"filename": "d.pdf", "size_bytes": 10},
        )
        assert ok_custom.status_code == 201 and custom_store.sessions[-1][3] == "https://rag.acme.example"
        unlisted = custom_client.post(
            f"/api/v1/uploads/{ws2}/files",
            headers={**jwt_hdr, "Host": "rag-arena-xxxx-uc.a.run.app", "Origin": "https://rag-arena-xxxx-uc.a.run.app"},
            json={"filename": "e.pdf", "size_bytes": 10},
        )
        assert unlisted.status_code == 400


def test_security_headers_and_externalized_theme_init_script() -> None:
    client = TestClient(create_app(settings=ArenaSettings(demo_mode="replay")))
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    csp = resp.headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "'unsafe-eval'" not in csp
    assert "connect-src 'self' https://storage.googleapis.com" in csp
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"
    assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert "camera=()" in resp.headers.get("permissions-policy", "")

    repo_root = Path(__file__).resolve().parents[2]
    index_html = (repo_root / "frontend" / "index.html").read_text(encoding="utf-8")
    assert '<script src="/theme-init.js"></script>' in index_html
    assert "localStorage" not in index_html
    assert (repo_root / "frontend" / "public" / "theme-init.js").is_file()
