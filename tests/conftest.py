"""Pytest Fixtures and Environment Initialization."""
import os
import sys
import warnings
import pytest
from pathlib import Path
from sqlalchemy.engine import make_url

# Filter out third-party library deprecations (e.g. RQ internal datetime.utcnow in Python 3.12)
warnings.filterwarnings("ignore", category=DeprecationWarning, message=r".*datetime.datetime.utcnow\(\) is deprecated.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="rq.*")
warnings.filterwarnings("ignore", category=ResourceWarning)

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(backend_dir))

# Studio tests must keep user data and generated output inside disposable test storage.
test_storage_root = Path(os.environ.get(
    "ABOODVIDEO_TEST_STORAGE_PATH",
    str(Path(__file__).resolve().parent / "test_storage"),
)).resolve()
test_local_app_data = (test_storage_root / "localappdata").resolve()
real_local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")).resolve()
if sys.platform == "win32":
    # Browser binaries are read-only shared tools, not a Studio user profile.
    # Resolve their cache before isolating LOCALAPPDATA below.
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(real_local_app_data / "ms-playwright"))
if test_local_app_data == real_local_app_data or real_local_app_data in test_local_app_data.parents:
    raise RuntimeError("Refusing to run tests with Studio profile overlapping real LOCALAPPDATA.")
test_local_app_data.mkdir(parents=True, exist_ok=True)
os.environ["LOCALAPPDATA"] = str(test_local_app_data)
os.environ["ABOODVIDEO_TEST_PROFILE_ROOT"] = str(test_local_app_data)

# Force test environment variables before importing app
os.environ["ENVIRONMENT"] = "test"
os.environ["DEBUG"] = "true"
os.environ["STORAGE_BASE_PATH"] = str(test_storage_root)
os.environ["STORAGE_UPLOADS_PATH"] = str(test_storage_root / "uploads")
os.environ["STORAGE_VIDEOS_PATH"] = str(test_storage_root / "videos")
os.environ["STORAGE_TEMP_PATH"] = str(test_storage_root / "temp")
os.environ["STORAGE_PACKAGES_STAGING_PATH"] = str(test_storage_root / "staging" / "packages")
os.environ["WEB_CONCURRENCY"] = "1"
os.environ["SECRET_KEY"] = "test-secret-key-for-unit-testing-32-bytes-long"
os.environ["IP_PRIVACY_SALT"] = "test-ip-privacy-salt-for-unit-testing-32-bytes"
os.environ["ALLOWED_HOSTS"] = "localhost,127.0.0.1,test,web"
test_database_url = os.environ.get("ABOODVIDEO_TEST_DATABASE_URL")
if not test_database_url:
    raise RuntimeError("Set ABOODVIDEO_TEST_DATABASE_URL to the isolated test PostgreSQL service before running pytest.")
test_db = make_url(test_database_url)
if (test_db.host not in {"127.0.0.1", "localhost"}
        or test_db.port not in {15432, 25432}
        or test_db.database != "aboodvideo_test"
        or test_db.drivername != "postgresql+asyncpg"):
    raise RuntimeError("Refusing pytest database outside the isolated loopback aboodvideo_test service.")
test_sync_db = test_db.set(drivername="postgresql+psycopg")
test_redis_urls = {
    "REDIS_QUEUE_URL": os.environ.get("ABOODVIDEO_TEST_REDIS_QUEUE_URL", ""),
    "REDIS_SESSION_URL": os.environ.get("ABOODVIDEO_TEST_REDIS_SESSION_URL", ""),
    "REDIS_CACHE_URL": os.environ.get("ABOODVIDEO_TEST_REDIS_CACHE_URL", ""),
}
for name, url in test_redis_urls.items():
    redis = make_url(url) if url else None
    if (redis is None or redis.drivername != "redis" or redis.host not in {"127.0.0.1", "localhost"}
            or redis.port not in {16379, 26379}):
        raise RuntimeError(f"Set {name.replace('REDIS_', 'ABOODVIDEO_TEST_REDIS_')} to isolated loopback Redis on port 16379.")
os.environ["POSTGRES_SERVER"] = test_db.host
os.environ["POSTGRES_PORT"] = str(test_db.port)
os.environ["POSTGRES_DB"] = test_db.database
os.environ["POSTGRES_USER"] = test_db.username or ""
os.environ["POSTGRES_PASSWORD"] = test_db.password or ""
os.environ["DATABASE_URL"] = test_db.render_as_string(hide_password=False)
os.environ["DATABASE_URL_SYNC"] = test_sync_db.render_as_string(hide_password=False)
os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "16379"
os.environ.update(test_redis_urls)

from app.core.config import settings
from app.main import create_application
from app.core.database import AsyncSessionLocal
from app.models.user import User, UserRole
from app.security.hashing import hash_password
import uuid


@pytest.fixture(scope="session", autouse=True)
def setup_test_storage():
    """Ensure test storage directory exists and is cleaned up."""
    test_storage = Path(settings.STORAGE_BASE_PATH)
    test_storage.mkdir(parents=True, exist_ok=True)
    yield
    import shutil
    if test_storage.exists():
        shutil.rmtree(test_storage, ignore_errors=True)


@pytest.fixture
def test_app():
    return create_application()


@pytest.fixture(autouse=True)
async def cleanup_connections():
    """Proper async connection lifecycle cleanup for SQLAlchemy asyncpg and Redis connection pools."""
    yield
    from app.core.database import close_db_connections
    from app.core.redis import close_redis_connections
    await close_db_connections()
    await close_redis_connections()


@pytest.fixture(autouse=True)
async def isolate_login_rate_limits():
    """Remove only login counters from Redis DB 2 so tests cannot share attempts."""
    try:
        from app.core.redis import get_cache_client

        async def clear_test_login_keys():
            client = get_cache_client()
            keys = [key async for key in client.scan_iter(match="rate_limit:login:*")]
            if keys:
                await client.delete(*keys)

        await clear_test_login_keys()
    except Exception:
        pass
    yield
    try:
        await clear_test_login_keys()
    except Exception:
        pass


@pytest.fixture
async def db_session():
    """Compatibility session for legacy Drive tests using the isolated pytest DB."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.rollback()


@pytest.fixture
async def test_user(db_session):
    """Deterministic explicit-owner fixture; teardown removes its owned test data."""
    user = User(
        id=uuid.uuid4(),
        email=f"drive_fixture_{uuid.uuid4().hex}@example.test",
        username=f"drive_fixture_{uuid.uuid4().hex}",
        hashed_password=hash_password("DriveFixturePassword2026!"),
        role=UserRole.OWNER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    try:
        yield user
    finally:
        await db_session.delete(user)
        await db_session.commit()
