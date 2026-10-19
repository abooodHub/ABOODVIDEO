"""Application Configuration Module."""
import os
from typing import Any, List, Literal, Union
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # Project metadata & environment
    PROJECT_NAME: str = "ABOODVIDEO"
    ENVIRONMENT: str = "production"
    APP_ENV: str = "development"  # "development" | "staging" | "production"
    DEBUG: bool = False
    API_V1_STR: str = "/api/v1"
    # These are deliberately required: a checked-in fallback would make every
    # deployment that omitted configuration share the same signing material.
    SECRET_KEY: str = Field(..., min_length=32, description="Cryptographic signing secret")
    IP_PRIVACY_SALT: str = Field(..., min_length=32, description="HMAC salt for client-IP privacy")

    # Network, Domain & Security (Never hardcode LAN IP in defaults)
    PUBLIC_BASE_URL: str = "http://localhost"
    DOMAIN: str = "localhost"
    ALLOWED_HOSTS: Union[str, List[str]] = "localhost,127.0.0.1,test"
    CORS_ORIGINS: Union[str, List[str]] = "http://localhost"
    SECURITY_MODE: Literal["lan", "public"] = "lan"
    FORCE_HTTPS: bool = False
    HSTS_MAX_AGE_SECONDS: int = 31536000
    HSTS_INCLUDE_SUBDOMAINS: bool = False
    TRUSTED_PROXIES: Union[str, List[str]] = Field(
        default="127.0.0.1,::1,172.28.0.0/16",
        description="Comma-separated list of trusted reverse proxy IPs or CIDR networks (strictly loopback & internal Docker bridge)",
    )

    # Server Execution & Concurrency
    WEB_CONCURRENCY: int = Field(
        default=1,
        description="Number of Uvicorn workers. Default is 1 for 2 vCPU environments.",
    )
    PORT: int = 8000

    # PostgreSQL Database Settings
    POSTGRES_SERVER: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "aboodvideo_db"
    POSTGRES_USER: str = "aboodvideo_user"
    POSTGRES_PASSWORD: str = Field(..., description="Required PostgreSQL password")
    DATABASE_POOL_SIZE: int = Field(default=10, ge=1)
    DATABASE_MAX_OVERFLOW: int = Field(default=5, ge=0)
    DATABASE_URL: str = Field(
        ...,
        description="Async SQLAlchemy database URL",
    )
    DATABASE_URL_SYNC: str = Field(
        ...,
        description="Sync SQLAlchemy database URL for Alembic migrations and CLI commands",
    )

    # Redis Logical Isolation (DB 0 = Queue, DB 1 = Sessions, DB 2 = Cache/RateLimit)
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str = ""
    REDIS_POOL_MAX_CONNECTIONS: int = Field(default=20, ge=1)
    REDIS_QUEUE_URL: str = "redis://redis:6379/0"
    REDIS_SESSION_URL: str = "redis://redis:6379/1"
    REDIS_CACHE_URL: str = "redis://redis:6379/2"

    # Storage paths & limits
    STORAGE_BASE_PATH: str = "/app/storage"
    STORAGE_UPLOADS_PATH: str = "/app/storage/uploads"
    STORAGE_VIDEOS_PATH: str = "/app/storage/videos"
    STORAGE_TEMP_PATH: str = "/app/storage/temp"
    STORAGE_PACKAGES_STAGING_PATH: str = "/app/storage/temp/packages"

    # Storage Safety Thresholds (5 GiB safety margin, non-destructive)
    STORAGE_MIN_FREE_BYTES: int = Field(
        default=5368709120,  # 5 GiB
        description="Minimum free storage floor in bytes",
    )
    STORAGE_SAFETY_MARGIN_BYTES: int = Field(
        default=5368709120,  # 5 GiB
        description="Explicit 5 GiB safety floor for admission calculations",
    )
    STORAGE_SOFT_THRESHOLD_PERCENT: float = Field(
        default=85.0,
        description="Percentage disk usage that triggers warnings in monitoring",
    )
    STORAGE_HARD_THRESHOLD_PERCENT: float = Field(
        default=95.0,
        description="Percentage disk usage that blocks new operations and marks health check unhealthy",
    )
    MAX_UPLOAD_SIZE_BYTES: int = 10737418240  # 10 GiB
    MAX_ACTIVE_UPLOADS_PER_USER: int = Field(
        default=0, ge=0, le=8,
        validation_alias=AliasChoices("MAX_ACTIVE_UPLOADS_PER_USER", "QA_MAX_ACTIVE_UPLOADS_PER_USER"),
        description="0 preserves unrestricted source-upload slots; 1..8 limits concurrent reservations",
    )
    UPLOAD_INACTIVITY_TIMEOUT_SECONDS: int = 3600
    SERVER_TRANSCODE_MULTIPLIER: float = 1.5
    SERVER_TRANSCODE_OVERHEAD_MULTIPLIER: float = 0.1

    # Strict Manifest Limits for Studio Packages
    MAX_MANIFEST_BYTES: int = 5242880  # 5 MiB
    MAX_PACKAGE_FILES: int = 20000
    MAX_PACKAGE_ARCHIVE_BYTES: int = 22548578304  # 21 GiB including tar headers around a 20 GiB payload
    MAX_TOTAL_PACKAGE_BYTES: int = 21474836480  # 20 GiB
    MAX_SINGLE_FILE_BYTES: int = 524288000  # 500 MiB
    MAX_PATH_LENGTH: int = 255
    MAX_RENDITIONS: int = 6
    MAX_SUBTITLES: int = 10
    MAX_SEGMENTS: int = 5000
    MAX_VIDEO_DURATION_SECONDS: float = 14400.0  # 4 hours
    MAX_VIDEO_WIDTH: int = 3840
    MAX_VIDEO_HEIGHT: int = 2160
    MAX_VIDEO_FPS: int = 60

    # Worker & Transcoding
    TRANSCODE_CONCURRENCY: int = 1
    FFMPEG_THREADS: int = 1
    FFMPEG_PRESET: str = "veryfast"
    TRANSCODE_JOB_TIMEOUT_SECONDS: int = 43200
    TRANSCODE_MAX_ATTEMPTS: int = 3
    TRANSCODE_PROGRESS_THROTTLE_SECONDS: float = 1.5
    TRANSCODE_STALL_TIMEOUT_SECONDS: int = 300
    TRANSCODE_STALL_CHECK_INTERVAL_SECONDS: float = 5.0
    HLS_SEGMENT_DURATION_SECONDS: int = 6
    ENABLE_TRANSCODING: bool = True
    ENABLE_DIRECT_IMPORT: bool = False
    DIRECT_IMPORT_MAX_BYTES: int = Field(default=2147483648, gt=0)
    ENABLE_4K_TRANSCODING: bool = True
    DELETE_SOURCE_AFTER_SUCCESS: bool = False
    DISK_SAFETY_MARGIN_BYTES: int = 5368709120  # 5 GiB

    # Security & Sessions
    SESSION_COOKIE_NAME: str = "aboodvideo_session"
    SESSION_COOKIE_SECURE: bool = False
    SESSION_COOKIE_SAMESITE: str = "strict"
    SESSION_MAX_AGE_SECONDS: int = 604800  # 7 days
    CSRF_COOKIE_NAME: str = "aboodvideo_csrf"
    CSRF_HEADER_NAME: str = "X-CSRF-Token"
    SIGNED_URL_TTL_SECONDS: int = 300  # 5 minutes

    # Rate Limiting Matrix
    RATE_LIMIT_LOGIN_ATTEMPTS: int = 5
    RATE_LIMIT_LOGIN_WINDOW_SECONDS: int = 900  # 15 minutes
    RATE_LIMIT_LOGIN_IP_ATTEMPTS: int = 25  # Failed attempts per IP across all accounts
    RATE_LIMIT_LOGIN_USER_ATTEMPTS: int = 10  # Failed attempts per user across all IPs
    RATE_LIMIT_VIEWER_PASSWORD_ATTEMPTS: int = 10
    RATE_LIMIT_VIEWER_PASSWORD_WINDOW_SECONDS: int = 600  # 10 minutes
    RATE_LIMIT_APIKEY_FAIL_ATTEMPTS: int = 20
    RATE_LIMIT_APIKEY_FAIL_WINDOW_SECONDS: int = 300  # 5 minutes
    RATE_LIMIT_STUDIO_INIT_ATTEMPTS: int = 120
    RATE_LIMIT_STUDIO_INIT_WINDOW_SECONDS: int = 3600  # 1 hour
    RATE_LIMIT_STUDIO_FINALIZE_ATTEMPTS: int = 120
    RATE_LIMIT_STUDIO_FINALIZE_WINDOW_SECONDS: int = 60  # 1 minute

    # Tier settings
    FREE_MAX_QUALITY: str = "720p"
    VIP_MAX_QUALITY: str = "1080p"

    # Media delivery
    SERVE_MEDIA_VIA_NGINX: bool = True

    # Secure Playback Sessions (short-lived, HttpOnly cookie authorized)
    PLAYBACK_SESSION_TTL_SECONDS: int = 900  # 15 minutes, sliding while watching
    PLAYBACK_COOKIE_NAME: str = "abood_playback"
    PLAYBACK_MAX_CONCURRENT_SESSIONS: int = 5  # per-principal active watch sessions
    PLAYBACK_SLIDING_REFRESH_SECONDS: int = 30  # throttle Redis writes per segment
    ENABLE_HLS_ENCRYPTION: bool = False  # Phase 1 AES-128 for NEW encodes only

    @field_validator("TRUSTED_PROXIES", mode="before")
    @classmethod
    def assemble_trusted_proxies(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            return [i.strip() for i in v.split(",") if i.strip()]
        return v

    @field_validator("ALLOWED_HOSTS", mode="before")
    @classmethod
    def assemble_allowed_hosts(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            return [i.strip() for i in v.split(",") if i.strip()]
        return v
    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            return [i.strip() for i in v.split(",") if i.strip()]
        return v

    @field_validator(
        "DATABASE_URL",
        "DATABASE_URL_SYNC",
        "REDIS_QUEUE_URL",
        "REDIS_SESSION_URL",
        "REDIS_CACHE_URL",
        "PUBLIC_BASE_URL",
        mode="before",
    )
    @classmethod
    def strip_quotes(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip("\"'")
        return v

    @model_validator(mode="after")
    def validate_public_security_mode(self) -> "Settings":
        """Keep LAN deployments usable while making public mode fail closed."""
        if self.SECURITY_MODE == "public":
            if not self.FORCE_HTTPS:
                raise ValueError("FORCE_HTTPS must be true when SECURITY_MODE=public")
            if not self.SESSION_COOKIE_SECURE:
                raise ValueError("SESSION_COOKIE_SECURE must be true when SECURITY_MODE=public")
            if not self.PUBLIC_BASE_URL.startswith("https://"):
                raise ValueError("PUBLIC_BASE_URL must use https:// when SECURITY_MODE=public")
        return self

    EXTERNAL_IDENTITY_SECRET: str = ""


    EXTERNAL_IDENTITY_ISSUER: str = ""


    EXTERNAL_IDENTITY_AUDIENCE: str = ""


    INSPECTOR_ALLOW_INSECURE_TLS: bool = False


    RECONCILER_INTERVAL_SECONDS: int = Field(default=300, ge=30, le=3600)


    ANALYTICS_RETENTION_DAYS: int = Field(default=365, ge=0, le=3650)


    ANALYTICS_RETENTION_BATCH_SIZE: int = Field(default=1000, ge=1, le=10000)



settings = Settings(_env_file=None if os.environ.get("ENVIRONMENT") == "test" else ".env")

# Quality ladder presets: Width x Height, target video bitrate in kbps, audio bitrate in kbps
LADDER_PRESETS = {
    "360p": {"width": 640, "height": 360, "video_bitrate_kbps": 700, "audio_bitrate_kbps": 96},
    "480p": {"width": 854, "height": 480, "video_bitrate_kbps": 1200, "audio_bitrate_kbps": 128},
    "720p": {"width": 1280, "height": 720, "video_bitrate_kbps": 2500, "audio_bitrate_kbps": 128},
    "1080p": {"width": 1920, "height": 1080, "video_bitrate_kbps": 4500, "audio_bitrate_kbps": 160},
    "1440p": {"width": 2560, "height": 1440, "video_bitrate_kbps": 9000, "audio_bitrate_kbps": 192},
    "2160p": {"width": 3840, "height": 2160, "video_bitrate_kbps": 16000, "audio_bitrate_kbps": 192},
}
