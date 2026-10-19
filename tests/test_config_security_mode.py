"""Secret and LAN/public transport configuration regressions."""
import pytest
from pydantic import ValidationError

from app.core.config import Settings


def base_settings(**overrides):
    values = {
        "SECRET_KEY": "s" * 32,
        "IP_PRIVACY_SALT": "p" * 32,
        "POSTGRES_PASSWORD": "db-password-for-isolated-test",
        "DATABASE_URL": "postgresql+asyncpg://user:password@localhost/test",
        "DATABASE_URL_SYNC": "postgresql+psycopg://user:password@localhost/test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_security_secrets_have_no_builtin_fallbacks(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("IP_PRIVACY_SALT", raising=False)
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            POSTGRES_PASSWORD="db-password-for-isolated-test",
            DATABASE_URL="postgresql+asyncpg://user:password@localhost/test",
            DATABASE_URL_SYNC="postgresql+psycopg://user:password@localhost/test",
        )


def test_lan_mode_permits_http_and_non_secure_cookies():
    settings = base_settings(SECURITY_MODE="lan", FORCE_HTTPS=False, SESSION_COOKIE_SECURE=False, PUBLIC_BASE_URL="http://192.168.1.20")
    assert settings.SECURITY_MODE == "lan"


@pytest.mark.parametrize("overrides", [
    {"FORCE_HTTPS": False, "SESSION_COOKIE_SECURE": True, "PUBLIC_BASE_URL": "https://example.test"},
    {"FORCE_HTTPS": True, "SESSION_COOKIE_SECURE": False, "PUBLIC_BASE_URL": "https://example.test"},
    {"FORCE_HTTPS": True, "SESSION_COOKIE_SECURE": True, "PUBLIC_BASE_URL": "http://example.test"},
])
def test_public_mode_rejects_incomplete_https_configuration(overrides):
    with pytest.raises(ValidationError):
        base_settings(SECURITY_MODE="public", **overrides)


def test_public_mode_accepts_https_and_secure_cookie_configuration():
    settings = base_settings(SECURITY_MODE="public", FORCE_HTTPS=True, SESSION_COOKIE_SECURE=True, PUBLIC_BASE_URL="https://example.test")
    assert settings.SECURITY_MODE == "public"
