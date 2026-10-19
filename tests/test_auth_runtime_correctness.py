"""Phase 6.0 authentication runtime and rate-limit regressions."""
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core.config import settings
from app.core.exceptions import AboodVideoException
from app.security import auth as auth_security


class FakePipeline:
    def __init__(self, store):
        self.store = store
        self.key = None

    def incr(self, key):
        self.key = key
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self

    def expire(self, key, seconds):
        self.store[f"{key}:ttl"] = seconds
        return self

    async def execute(self):
        return []


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def ttl(self, key):
        return self.store.get(f"{key}:ttl", -1)

    def pipeline(self):
        return FakePipeline(self.store)

    async def delete(self, key):
        self.store.pop(key, None)
        self.store.pop(f"{key}:ttl", None)


def login_request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/v1/auth/login", "headers": [], "client": ("127.0.0.1", 12345)})


@pytest.mark.asyncio
async def test_login_rate_limit_uses_canonical_settings_and_retry_after(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(auth_security, "get_cache_client", lambda: redis)
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_ATTEMPTS", 2)
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_WINDOW_SECONDS", 90)
    request = login_request()

    await auth_security.record_failed_login_attempt(request)
    await auth_security.record_failed_login_attempt(request)
    with pytest.raises(HTTPException) as caught:
        await auth_security.check_login_rate_limit(request)

    assert caught.value.status_code == 429
    assert caught.value.headers["Retry-After"] == "90"
    assert not hasattr(settings, "LOGIN_RATE_LIMIT_MAX_ATTEMPTS")


@pytest.mark.asyncio
async def test_successful_login_rate_limit_reset_clears_attempts(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(auth_security, "get_cache_client", lambda: redis)
    request = login_request()

    await auth_security.record_failed_login_attempt(request)
    await auth_security.reset_login_rate_limit(request)
    await auth_security.check_login_rate_limit(request)
    assert not redis.store


@pytest.mark.asyncio
async def test_redis_rate_limit_failure_is_safe_service_unavailable(monkeypatch):
    monkeypatch.setattr(auth_security, "get_cache_client", lambda: (_ for _ in ()).throw(OSError("isolated redis outage")))

    with pytest.raises(AboodVideoException) as caught:
        await auth_security.check_login_rate_limit(login_request())

    assert caught.value.status_code == 503
    assert caught.value.error_code == "SYSTEM_UNAVAILABLE"
