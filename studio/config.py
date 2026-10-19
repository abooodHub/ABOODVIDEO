"""Configuration, paths, logging and data migration for ABOODVIDEO Studio."""
import os
from studio import __version__
import sys
import json
import shutil
import tempfile
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Any

VERSION = __version__
APP_NAME = "ABOODVIDEO Studio"

# Determine base directories (supporting PyInstaller frozen mode)
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    STUDIO_ROOT = Path(sys._MEIPASS) / "studio"
    EXE_DIR = Path(sys.executable).parent
else:
    STUDIO_ROOT = Path(__file__).resolve().parent
    EXE_DIR = STUDIO_ROOT.parent

PROJECT_ROOT = STUDIO_ROOT.parent
FONTS_DIR = STUDIO_ROOT / "fonts"


def get_user_data_dir() -> Path:
    """Returns persistent application data directory: %LOCALAPPDATA%\\ABOODVIDEO Studio\\"""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data)
    else:
        base = Path.home() / "AppData" / "Local"
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


USER_DATA_DIR = get_user_data_dir()
MANAGED_FONTS_DIR = USER_DATA_DIR / "fonts"
CONFIG_FILE = USER_DATA_DIR / "config.json"
SECRETS_FILE = USER_DATA_DIR / "secrets.dat"
LOGS_DIR = USER_DATA_DIR / "logs"
TEMP_DIR = USER_DATA_DIR / "temp"
LOCK_FILE = USER_DATA_DIR / "studio.lock"
DEFAULT_DB_PATH = USER_DATA_DIR / "jobs.db"

# Bundled fonts are optional, read-only installation assets. Runtime writes go
# under the user profile, including on machines where Program Files is read-only.
MANAGED_FONTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "server_url": "",
    "api_key": "",
    "studio_port": 8555,
    "default_encoder": "auto",
    "encoder_preference": "balanced",
    "processing_mode": "local",
    "converted_output_folder": "",
    "font_library_folder": r"D:\fonts",
    "output_organization": "automatic",
    "cpu_concurrent_jobs": 1,
    "gpu_concurrent_jobs": 1,
    "default_preset": "balanced",
    "include_360p": True,
    "first_run_complete": False,
    "hls_segment_duration": 6,
    "concurrent_uploads": 3,
    "auto_cleanup_package": False,
    "secret_storage": "dpapi_current_user",
    "secret_version": 1,
}


def validate_converted_output_folder(path_value: Any, *, create: bool = False) -> Path:
    """Validate the user-owned destination used for completed media packages."""
    raw = str(path_value or "").strip().strip('"')
    if not raw:
        raise ValueError("اختر مجلد حفظ النسخ المحوّلة أولاً.")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError("يجب أن يكون مجلد الإخراج مساراً كاملاً.")
    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise ValueError("مجلد الإخراج المحدد غير موجود.")
    temp_root = TEMP_DIR.resolve()
    user_data_root = USER_DATA_DIR.resolve()
    if resolved == temp_root or temp_root in resolved.parents:
        raise ValueError("لا يمكن استخدام مجلد الملفات المؤقتة كمجلد إخراج نهائي.")
    if resolved == user_data_root:
        raise ValueError("اختر مجلداً مستقلاً خارج بيانات التطبيق.")
    try:
        with tempfile.NamedTemporaryFile(prefix=".aboodvideo-write-test-", dir=resolved):
            pass
    except OSError as exc:
        raise ValueError("مجلد الإخراج غير قابل للكتابة.") from exc
    return resolved


def get_converted_output_root(*, require_configured: bool = True) -> Path:
    """Return the persisted permanent output root."""
    value = load_studio_config().get("converted_output_folder", "")
    if not value and not require_configured:
        return TEMP_DIR
    return validate_converted_output_folder(value)


def save_secure_api_key(api_key: str) -> None:
    """Encrypts and persists the API key using Windows DPAPI (CurrentUser)."""
    from studio.core.dpapi import dpapi_protect
    if not api_key:
        if SECRETS_FILE.exists():
            try:
                SECRETS_FILE.unlink()
            except Exception:
                pass
        return

    encrypted_bytes = dpapi_protect(api_key.encode("utf-8"))
    with open(SECRETS_FILE, "wb") as f:
        f.write(encrypted_bytes)


def load_secure_api_key() -> str:
    """Loads and decrypts the API key from Windows DPAPI secret store."""
    from studio.core.dpapi import dpapi_unprotect
    if not SECRETS_FILE.exists():
        return ""
    try:
        with open(SECRETS_FILE, "rb") as f:
            encrypted_bytes = f.read()
        if not encrypted_bytes:
            return ""
        decrypted_bytes = dpapi_unprotect(encrypted_bytes)
        return decrypted_bytes.decode("utf-8")
    except Exception as e:
        logger = logging.getLogger("aboodvideo_studio")
        logger.warning("Failed to decrypt secure API key: %s", type(e).__name__)
        return ""


def migrate_user_data() -> None:
    """Migrate legacy configuration and move any plaintext API key into DPAPI.

    Studio 0.3 intentionally starts with a fresh queue. Development or legacy
    jobs.db files are never imported into a clean user profile.
    """
    try:
        # 1. Migrate legacy config from ~/.aboodvideo_studio
        old_cfg = Path.home() / ".aboodvideo_studio" / "config.json"
        if not CONFIG_FILE.exists() and old_cfg.exists():
            shutil.copy2(old_cfg, CONFIG_FILE)

        # 2. Secure DPAPI migration: encrypt a legacy plaintext key and sanitize config.json.
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg_data = json.load(f)
                if "api_key" in cfg_data:
                    raw_key = cfg_data.pop("api_key")
                    if raw_key and isinstance(raw_key, str) and raw_key.strip():
                        if not SECRETS_FILE.exists():
                            save_secure_api_key(raw_key.strip())
                    cfg_data["secret_storage"] = "dpapi_current_user"
                    cfg_data["secret_version"] = 1
                    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                        json.dump(cfg_data, f, indent=2, ensure_ascii=False)
            except Exception:
                pass
    except Exception:
        pass


# Run initial migration check
migrate_user_data()


def get_default_db_path() -> Path:
    """Returns authoritative path to SQLite jobs.db in %LOCALAPPDATA%."""
    return DEFAULT_DB_PATH


def load_studio_config() -> Dict[str, Any]:
    """Load studio config from disk or return defaults, decrypting DPAPI API key."""
    data = {}
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Immediate migration if plaintext api_key is encountered
            if "api_key" in data:
                raw_key = data.pop("api_key")
                if raw_key and isinstance(raw_key, str) and raw_key.strip():
                    if not SECRETS_FILE.exists():
                        save_secure_api_key(raw_key.strip())
                data["secret_storage"] = "dpapi_current_user"
                data["secret_version"] = 1
                try:
                    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                except Exception:
                    pass
        except Exception:
            pass

    config = {**DEFAULT_CONFIG, **data}
    # Populate decrypted API key from Windows DPAPI CurrentUser secret store
    config["api_key"] = load_secure_api_key()
    return config


def save_studio_config(config_data: Dict[str, Any]) -> None:
    """Save updated config to disk. API key is encrypted via Windows DPAPI and stripped from config.json."""
    current = load_studio_config()
    current.update(config_data)

    # 1. If api_key is updated in config_data, secure it via DPAPI
    if "api_key" in config_data:
        new_key = config_data.get("api_key", "")
        save_secure_api_key(str(new_key or "").strip())

    # 2. Build sanitized config for config.json (NO plaintext api_key!)
    to_save = dict(current)
    to_save.pop("api_key", None)
    to_save["secret_storage"] = "dpapi_current_user"
    to_save["secret_version"] = 1

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(to_save, f, indent=2, ensure_ascii=False)


def setup_studio_logging() -> logging.Logger:
    """Configure rotating file logger in %LOCALAPPDATA%\\ABOODVIDEO Studio\\logs\\ and console."""
    log_file = LOGS_DIR / "studio.log"
    logger = logging.getLogger("aboodvideo_studio")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        file_handler = RotatingFileHandler(
            str(log_file),
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        if sys.stdout is not None and not getattr(sys, "frozen", False):
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)

    return logger
