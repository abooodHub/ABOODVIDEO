"""Run source fixes against disposable loopback services only."""
import os
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
browser_cache = Path(os.environ.get("LOCALAPPDATA", str(Path.home()/"AppData/Local"))) / "ms-playwright"
if browser_cache.is_dir(): os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_cache)
storage = Path(tempfile.mkdtemp(prefix="abv-f16-test-"))
os.environ.update({
    "ENVIRONMENT":"test", "DEBUG":"true", "SECURITY_MODE":"lan", "FORCE_HTTPS":"false",
    "ABOODVIDEO_TEST_DATABASE_URL":"postgresql+asyncpg://fixture:fixture_only@127.0.0.1:25432/aboodvideo_test",
    "DATABASE_URL":"postgresql+asyncpg://fixture:fixture_only@127.0.0.1:25432/aboodvideo_test",
    "DATABASE_URL_SYNC":"postgresql+psycopg://fixture:fixture_only@127.0.0.1:25432/aboodvideo_test",
    "POSTGRES_SERVER":"127.0.0.1", "POSTGRES_PORT":"25432", "POSTGRES_DB":"aboodvideo_test", "POSTGRES_USER":"fixture", "POSTGRES_PASSWORD":"fixture_only",
    "SECRET_KEY":"synthetic-fixture-only-signing-material-2026", "IP_PRIVACY_SALT":"synthetic-fixture-only-privacy-material-2026",
    "ALLOWED_HOSTS":"localhost,127.0.0.1,testserver,test,web", "TRUSTED_PROXIES":"127.0.0.1,::1,172.28.0.0/16",
    "ABOODVIDEO_TEST_STORAGE_PATH":str(storage), "STORAGE_BASE_PATH":str(storage),
    "LOCALAPPDATA":str(storage / "profile"),
})
for name, suffix in {"STORAGE_UPLOADS_PATH":"uploads","STORAGE_VIDEOS_PATH":"videos","STORAGE_TEMP_PATH":"temp","STORAGE_PACKAGES_STAGING_PATH":"staging/packages"}.items():
    os.environ[name] = str(storage / suffix)
for key, db in {"QUEUE":0,"SESSION":1,"CACHE":2}.items():
    value = f"redis://127.0.0.1:26379/{db}"
    os.environ[f"REDIS_{key}_URL"] = value
    os.environ[f"ABOODVIDEO_TEST_REDIS_{key}_URL"] = value
sys.path[:0] = [str(root),str(root / "backend")]
os.chdir(storage)  # Never load a production .env, even in old modules.
from alembic.config import Config
from alembic import command
cfg = Config(str(root / "backend/alembic.ini"))
cfg.set_main_option("script_location",str(root / "backend/migrations"))
command.upgrade(cfg,"head")
os.chdir(root)
import pytest
raise SystemExit(pytest.main(sys.argv[1:] or ["tests/test_f16_regressions.py","-q"]))
