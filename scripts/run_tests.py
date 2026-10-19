"""Migrate and test only a disposable loopback PostgreSQL/Redis environment."""
from pathlib import Path
import os
import subprocess
import sys

from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


def test_environment():
    env = os.environ.copy()
    url = env.get("ABOODVIDEO_TEST_DATABASE_URL", "")
    database = make_url(url)
    if (database.drivername != "postgresql+asyncpg"
            or database.host not in {"127.0.0.1", "localhost"}
            or database.port not in {15432, 25432}
            or database.database != "aboodvideo_test"):
        raise ValueError("Use only an isolated loopback aboodvideo_test database on 15432 or 25432")
    for name in ("QUEUE", "SESSION", "CACHE"):
        value = env.get(f"ABOODVIDEO_TEST_REDIS_{name}_URL", "")
        redis = make_url(value)
        if (redis.drivername != "redis" or redis.host not in {"127.0.0.1", "localhost"}
                or redis.port not in {16379, 26379}):
            raise ValueError("Use only isolated loopback Redis on 16379 or 26379")
        env[f"REDIS_{name}_URL"] = value
    storage = Path(env.get("ABOODVIDEO_TEST_STORAGE_PATH", ROOT / "tests/test_storage")).resolve()
    env.update(
        ENVIRONMENT="test", DEBUG="true", SECURITY_MODE="lan", FORCE_HTTPS="false",
        SECRET_KEY="isolated-test-signing-secret-with-32-characters",
        IP_PRIVACY_SALT="isolated-test-privacy-salt-with-32-characters",
        POSTGRES_PASSWORD=database.password or "",
        DATABASE_URL=database.render_as_string(hide_password=False),
        DATABASE_URL_SYNC=database.set(drivername="postgresql+psycopg").render_as_string(hide_password=False),
        ALLOWED_HOSTS="localhost,127.0.0.1,test,web", PYTHONPATH=str(ROOT / "backend"),
        STORAGE_BASE_PATH=str(storage), STORAGE_UPLOADS_PATH=str(storage / "uploads"),
        STORAGE_VIDEOS_PATH=str(storage / "videos"), STORAGE_TEMP_PATH=str(storage / "temp"),
        STORAGE_PACKAGES_STAGING_PATH=str(storage / "staging/packages"),
    )
    return env


def main():
    env = test_environment()
    # The application schema must come from migrations, never create_all().
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                   cwd=ROOT / "backend", env=env, check=True)
    return subprocess.call([sys.executable, "-m", "pytest", *sys.argv[1:]], cwd=ROOT, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
