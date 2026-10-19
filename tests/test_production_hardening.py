"""
Tests for Phase 5 Production Hardening:
- Request ID middleware (sanitization and propagation)
- Security headers (CSP, HSTS conditional, Frame Options)
- Safe IP hashing (HMAC-SHA256 with salt)
- Safe logging & path redaction
- Storage admission mathematics (per-filesystem st_dev check)
- Health, Readiness, and Worker monitoring endpoints
"""

import os
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import create_application
from app.core.config import settings
from app.core.logging import hash_client_ip, redact_sensitive_url
from app.utils.disk import calculate_server_transcode_admission, check_package_storage_admission


@pytest.fixture
def app():
    return create_application()


@pytest.mark.asyncio
async def test_request_id_middleware_generates_uuid_when_missing(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        res = await client.get("/health")
        assert res.status_code == 200
        req_id = res.headers.get("X-Request-ID")
        assert req_id is not None
        assert len(req_id) >= 16


@pytest.mark.asyncio
async def test_request_id_middleware_echoes_valid_client_id(app):
    valid_id = "client-req-abc-12345678"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        res = await client.get("/health", headers={"X-Request-ID": valid_id})
        assert res.status_code == 200
        assert res.headers.get("X-Request-ID") == valid_id


@pytest.mark.asyncio
async def test_request_id_middleware_sanitizes_malicious_id(app):
    malicious_id = "bad<script>alert(1)</script>"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        res = await client.get("/health", headers={"X-Request-ID": malicious_id})
        assert res.status_code == 200
        echoed = res.headers.get("X-Request-ID")
        assert echoed != malicious_id
        assert len(echoed) >= 16


@pytest.mark.asyncio
async def test_security_headers_middleware(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        res = await client.get("/health")
        assert res.status_code == 200

        headers = res.headers
        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert headers.get("X-Frame-Options") == "DENY"
        assert headers.get("Referrer-Policy") == "no-referrer"

        csp = headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp
        assert "media-src 'self' blob:" in csp
        assert "frame-ancestors 'none'" in csp
        assert "script-src 'self'" in csp


def test_safe_client_ip_hashing():
    raw_ip = "192.168.1.100"
    h1 = hash_client_ip(raw_ip)
    h2 = hash_client_ip(raw_ip)

    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex
    assert raw_ip not in h1

    different_ip = "192.168.1.101"
    h3 = hash_client_ip(different_ip)
    assert h1 != h3


def test_redact_sensitive_url():
    raw_playback_url = "/api/v1/playback/v1_abc123456789opaque_token_string/master.m3u8"
    redacted = redact_sensitive_url(raw_playback_url)
    assert "abc123456789" not in redacted
    assert "/api/v1/playback/[REDACTED]/master.m3u8" == redacted


def test_storage_admission_server_transcode(tmp_path):
    # Free space calculation should not double-count existing active generations
    res = calculate_server_transcode_admission(
        source_file_bytes=100 * 1024 * 1024,
        existing_active_generation_bytes=150 * 1024 * 1024
    )
    assert "additional_free_bytes_required" in res
    assert "estimated_new_generation_bytes" in res
    assert "safety_margin_bytes" in res
    # Active generation is NOT added to additional free bytes required
    assert res["additional_free_bytes_required"] < 150 * 1024 * 1024 + 5 * 1024 * 1024 * 1024 + 500 * 1024 * 1024


def test_storage_admission_package_upload(tmp_path):
    staging_dir = tmp_path / "staging"
    storage_dir = tmp_path / "videos"
    staging_dir.mkdir()
    storage_dir.mkdir()

    admissible, info = check_package_storage_admission(
        package_total_bytes=50 * 1024 * 1024,
        staging_dir=staging_dir,
        storage_dir=storage_dir
    )
    assert isinstance(admissible, bool)
    assert "staging_free_bytes" in info
    assert "storage_free_bytes" in info
    assert "same_filesystem" in info


@pytest.mark.asyncio
async def test_health_and_readiness_endpoints(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        # Liveness
        res = await client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data.get("status") == "ok"

        # Alias liveness
        res2 = await client.get("/api/v1/health")
        assert res2.status_code == 200

        # Readiness
        res3 = await client.get("/readiness")
        assert res3.status_code in [200, 503]
        r_data = res3.json()
        assert "database" in r_data
        assert "redis" in r_data
        assert "storage" in r_data


@pytest.mark.asyncio
async def test_worker_health_requires_authorization(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        res = await client.get("/api/v1/health/worker")
        assert res.status_code == 401
