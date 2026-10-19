"""Health Check Endpoints HTTP Integration Tests."""
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from test_admin_auth_and_management import admin_fixture


@pytest.mark.asyncio
async def test_health_storage_endpoint(admin_fixture):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        assert (await client.get("/api/v1/health/storage")).status_code == 401
        login = await client.post("/api/v1/auth/login", json={"username_or_email": admin_fixture["owner"].username, "password": admin_fixture["owner_pass"]})
        assert login.status_code == 200
        response = await client.get("/api/v1/health/storage")
        assert response.status_code in [200, 507]
        data = response.json()
        assert data["service"] == "LocalStorage"
        assert "details" in data
        assert "free_bytes" in data["details"]
        assert "probes" in data["details"]


@pytest.mark.asyncio
async def test_health_db_endpoint(admin_fixture):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        assert (await client.get("/api/v1/health/db")).status_code == 401
        login = await client.post("/api/v1/auth/login", json={"username_or_email": admin_fixture["owner"].username, "password": admin_fixture["owner_pass"]})
        assert login.status_code == 200
        response = await client.get("/api/v1/health/db")
        assert response.status_code in [200, 503]
        data = response.json()
        assert data["service"] == "PostgreSQL"
        assert "latency_ms" in data["details"]


@pytest.mark.asyncio
async def test_health_redis_endpoint(admin_fixture):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        assert (await client.get("/api/v1/health/redis")).status_code == 401
        login = await client.post("/api/v1/auth/login", json={"username_or_email": admin_fixture["owner"].username, "password": admin_fixture["owner_pass"]})
        assert login.status_code == 200
        response = await client.get("/api/v1/health/redis")
        assert response.status_code in [200, 503]
        data = response.json()
        assert data["service"] == "Redis"
        assert "instances" in data["details"]


@pytest.mark.asyncio
async def test_health_overall_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get("/api/v1/health")
        assert response.status_code in [200, 503]
        data = response.json()
        assert "status" in data
        assert "services" in data


@pytest.mark.asyncio
async def test_health_worker_role_regression():
    """
    Regression Test for Priority 4:
    Verifies /api/v1/health/worker correctly checks UserRole enum for OWNER and ADMIN.
    Rejects anonymous and regular USER sessions.
    """
    import uuid
    import time
    import secrets
    from datetime import datetime, timezone, timedelta
    from redis import Redis
    from app.core.config import settings
    from app.core.database import get_db_context
    from app.models.user import User, UserRole
    from app.models.session import UserSession
    from app.security.hashing import hash_password
    from app.security.tokens import hash_token

    # Seed mock worker heartbeat in Redis
    sync_redis = Redis.from_url(settings.REDIS_QUEUE_URL)
    w_id = f"test-worker-{uuid.uuid4().hex[:6]}"
    sync_redis.sadd("worker:registered_workers", w_id)
    sync_redis.set(f"worker:last_seen:{w_id}", str(time.time()))
    sync_redis.set(f"worker:heartbeat:{w_id}", "1", ex=60)

    try:
        # Create OWNER, ADMIN, and regular USER in DB
        async with get_db_context() as db:
            owner = User(
                username=f"owner_{uuid.uuid4().hex[:6]}",
                email=f"owner_{uuid.uuid4().hex[:6]}@test.com",
                hashed_password=hash_password("Pass123!"),
                role=UserRole.OWNER,
                is_active=True,
            )
            admin = User(
                username=f"admin_{uuid.uuid4().hex[:6]}",
                email=f"admin_{uuid.uuid4().hex[:6]}@test.com",
                hashed_password=hash_password("Pass123!"),
                role=UserRole.ADMIN,
                is_active=True,
            )
            user = User(
                username=f"user_{uuid.uuid4().hex[:6]}",
                email=f"user_{uuid.uuid4().hex[:6]}@test.com",
                hashed_password=hash_password("Pass123!"),
                role=UserRole.USER,
                is_active=True,
            )
            db.add_all([owner, admin, user])
            await db.commit()
            await db.refresh(owner)
            await db.refresh(admin)
            await db.refresh(user)

            # Create sessions
            tok_owner, tok_admin, tok_user = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            db.add_all([
                UserSession(user_id=owner.id, token_hash=hash_token(tok_owner), expires_at=datetime.now(timezone.utc) + timedelta(days=1)),
                UserSession(user_id=admin.id, token_hash=hash_token(tok_admin), expires_at=datetime.now(timezone.utc) + timedelta(days=1)),
                UserSession(user_id=user.id, token_hash=hash_token(tok_user), expires_at=datetime.now(timezone.utc) + timedelta(days=1)),
            ])
            await db.commit()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
            # 1. Anonymous -> 401
            res_anon = await client.get("/api/v1/health/worker")
            assert res_anon.status_code == 401

            # 2. Regular USER session -> 401 (not authorized)
            res_user = await client.get(
                "/api/v1/health/worker",
                cookies={settings.SESSION_COOKIE_NAME: tok_user}
            )
            assert res_user.status_code == 401

            # 3. ADMIN session -> 200 OK
            res_admin = await client.get(
                "/api/v1/health/worker",
                cookies={settings.SESSION_COOKIE_NAME: tok_admin}
            )
            assert res_admin.status_code == 200
            assert res_admin.json()["status"] in ("ok", "healthy")
            assert res_admin.json()["worker_status"] == "ONLINE"

            # 4. OWNER session -> 200 OK
            res_owner = await client.get(
                "/api/v1/health/worker",
                cookies={settings.SESSION_COOKIE_NAME: tok_owner}
            )
            assert res_owner.status_code == 200
            assert res_owner.json()["status"] in ("ok", "healthy")
            assert res_owner.json()["worker_status"] == "ONLINE"

    finally:
        sync_redis.srem("worker:registered_workers", w_id)
        sync_redis.delete(f"worker:last_seen:{w_id}", f"worker:heartbeat:{w_id}")
