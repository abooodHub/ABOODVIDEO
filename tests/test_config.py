"""Configuration and Settings Unit Tests."""
import pytest
from pydantic import ValidationError
from app.core.config import Settings


def test_default_settings():
    s = Settings()
    # Rule 1: Uvicorn workers default to 1 for 2 vCPU environment
    assert s.WEB_CONCURRENCY == 1

    # Rule 6: Logical separation of Redis instances
    assert s.REDIS_QUEUE_URL.endswith("/0")
    assert s.REDIS_SESSION_URL.endswith("/1")
    assert s.REDIS_CACHE_URL.endswith("/2")

    # Rule 5: Storage threshold settings
    assert s.STORAGE_SOFT_THRESHOLD_PERCENT == 85.0
    assert s.STORAGE_HARD_THRESHOLD_PERCENT == 95.0
    assert s.STORAGE_MIN_FREE_BYTES == 5368709120  # 5 GiB


def test_allowed_hosts_and_cors_parsing():
    s = Settings(
        ALLOWED_HOSTS="example.com, api.example.com",
        CORS_ORIGINS="http://localhost:3000, https://example.com",
    )
    assert s.ALLOWED_HOSTS == ["example.com", "api.example.com"]
    assert s.CORS_ORIGINS == ["http://localhost:3000", "https://example.com"]


def test_database_secrets_are_required_without_environment_file(monkeypatch):
    for name in ("POSTGRES_PASSWORD", "DATABASE_URL", "DATABASE_URL_SYNC"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_explicit_database_configuration_is_accepted_without_environment_file():
    settings = Settings(
        _env_file=None,
        POSTGRES_PASSWORD="test-only-password",
        DATABASE_URL="postgresql+asyncpg://user:test@db:5432/test",
        DATABASE_URL_SYNC="postgresql+psycopg://user:test@db:5432/test",
    )

    assert settings.POSTGRES_PASSWORD == "test-only-password"
