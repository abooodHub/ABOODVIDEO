"""Structured Logging and Privacy-Preserving Telemetry Configuration."""
import hmac
import hashlib
import json
import logging
import re
import sys
from typing import Optional
from app.core.config import settings


def hash_client_ip(ip: Optional[str]) -> str:
    """
    Derives a privacy-safe client IP identifier using keyed HMAC-SHA256.
    Protects against IPv4 dictionary enumeration and rainbow-table attacks.
    """
    if not ip or ip == "unknown":
        return "unknown"
    return hmac.new(
        settings.IP_PRIVACY_SALT.encode("utf-8"),
        ip.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def sanitize_path_for_logging(path: str) -> str:
    """
    Redacts sensitive segments from URL paths (e.g. playback tokens, secrets).
    Example: /api/v1/playback/v_tok_abc123/master.m3u8 -> /api/v1/playback/[REDACTED]/master.m3u8
    """
    # Redact opaque playback token in /api/v1/playback/{token}/...
    path = re.sub(r"(/api/v1/playback/(?:(?:p|s)/)?)([^/]+)(.*)", r"\1[REDACTED]\3", path)
    path = re.sub(r"(/private-media/)([^/]+)(.*)", r"\1[REDACTED]\3", path)
    # Redact raw API keys if accidentally passed in URL
    path = re.sub(r"(ak_live_[a-zA-Z0-9_\-]+)", "[REDACTED_KEY]", path)
    return path


redact_sensitive_url = sanitize_path_for_logging


def setup_logging() -> logging.Logger:
    log_level = logging.DEBUG if settings.DEBUG else logging.INFO
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    if not root_logger.handlers:
        root_logger.addHandler(handler)
    else:
        root_logger.handlers = [handler]

    # Suppress external verbose loggers in production
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    logger = logging.getLogger("aboodvideo")
    logger.setLevel(log_level)
    return logger


logger = setup_logging()


def get_logger(name: str = "aboodvideo") -> logging.Logger:
    return logging.getLogger(name)
