"""Core Video, Rendition, and TranscodeJob Models for Phase 3 Production Transcoding."""
import enum
import uuid
from datetime import datetime
from typing import Optional, List, TYPE_CHECKING
import sqlalchemy as sa
from sqlalchemy import (
    String,
    Text,
    BigInteger,
    Integer,
    Boolean,
    Enum,
    ForeignKey,
    Float,
    DateTime,
    UniqueConstraint,
    Index,
    JSON,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.upload import UploadSession
    from app.models.folder import Folder
    from app.models.series import Series, Season


class VideoStatus(str, enum.Enum):
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    QUEUED = "QUEUED"
    PROBING = "PROBING"
    PROCESSING = "PROCESSING"
    VERIFYING = "VERIFYING"
    READY = "READY"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class VideoVisibility(str, enum.Enum):
    PRIVATE = "PRIVATE"
    UNLISTED = "UNLISTED"
    PUBLIC = "PUBLIC"
    PASSWORD_PROTECTED = "PASSWORD_PROTECTED"


class TranscodeJobStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    PROBING = "PROBING"
    PROCESSING = "PROCESSING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Video(Base, TimestampMixin):
    __tablename__ = "videos"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    series_name: Mapped[Optional[str]] = mapped_column(String(150), nullable=True, index=True)
    season_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    episode_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[VideoStatus] = mapped_column(
        Enum(VideoStatus, name="video_status_enum", native_enum=True),
        default=VideoStatus.UPLOADING,
        nullable=False,
        index=True,
    )
    source_filename: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    source_size_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
    )

    is_test_content: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # Phase 2 metadata expansion
    source_duration_seconds: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    source_resolution_width: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )
    source_resolution_height: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )
    source_bitrate: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
    )
    source_codec: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
    )
    source_path: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
    )
    upload_progress_percent: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False,
    )

    # Visibility & Management (Phase 2 extension)
    visibility: Mapped[VideoVisibility] = mapped_column(
        Enum(VideoVisibility, name="video_visibility_enum", native_enum=True),
        default=VideoVisibility.PRIVATE,
        nullable=False,
        index=True,
    )
    share_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        unique=True,
        index=True,
        nullable=True,
    )
    share_revoked: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    viewer_password_hash: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )
    poster_path: Mapped[Optional[str]] = mapped_column(
        String(1024),
        nullable=True,
    )
    view_count: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
    )
    bandwidth_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
    )

    # Phase 3 Transcoding & Generation Extensions
    active_generation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    master_playlist_path: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
    )
    transcode_progress_percent: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False,
    )

    # Library Drive & Folder Organization
    folder_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("folders.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    series_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("series.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    season_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("seasons.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    allow_download: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    allow_download_policy: Mapped[str] = mapped_column(
        String(16),
        default="INHERIT",  # INHERIT, ALLOW, DENY
        nullable=False,
    )
    trashed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="videos")
    folder: Mapped[Optional["Folder"]] = relationship("Folder", back_populates="videos")
    series: Mapped[Optional["Series"]] = relationship("Series", back_populates="videos")
    season: Mapped[Optional["Season"]] = relationship("Season", back_populates="episodes")
    renditions: Mapped[List["VideoRendition"]] = relationship(
        "VideoRendition",
        back_populates="video",
        cascade="all, delete-orphan",
    )
    upload_sessions: Mapped[List["UploadSession"]] = relationship(
        "UploadSession",
        back_populates="video",
        cascade="all, delete-orphan",
    )
    transcode_jobs: Mapped[List["TranscodeJob"]] = relationship(
        "TranscodeJob",
        back_populates="video",
        cascade="all, delete-orphan",
        order_by="desc(TranscodeJob.created_at)",
    )
    subtitles: Mapped[List["VideoSubtitle"]] = relationship(
        "VideoSubtitle",
        back_populates="video",
        cascade="all, delete-orphan",
        order_by="VideoSubtitle.created_at",
    )
    font_assets: Mapped[List["VideoFontAsset"]] = relationship(
        "VideoFontAsset",
        back_populates="video",
        cascade="all, delete-orphan",
        order_by="VideoFontAsset.created_at",
    )
    playback_sessions: Mapped[List["PlaybackSession"]] = relationship(
        "PlaybackSession",
        back_populates="video",
        cascade="all, delete-orphan",
        order_by="desc(PlaybackSession.started_at)",
    )


class VideoRendition(Base, TimestampMixin):
    __tablename__ = "video_renditions"
    __table_args__ = (
        UniqueConstraint("video_id", "quality_label", name="uq_video_renditions_video_id_quality_label"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("videos.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    quality_label: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
    )
    resolution_width: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    resolution_height: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    is_vip_only: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    # Phase 3 Adaptive HLS Extensions
    video_codec: Mapped[str] = mapped_column(
        String(32),
        default="h264",
        nullable=False,
    )
    audio_codec: Mapped[Optional[str]] = mapped_column(
        String(32),
        nullable=True,
    )
    video_bitrate: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )
    audio_bitrate: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )
    playlist_path: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
    )
    size_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
    )
    is_ready: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    video: Mapped["Video"] = relationship("Video", back_populates="renditions")


class TranscodeJob(Base, TimestampMixin):
    __tablename__ = "transcode_jobs"
    __table_args__ = (
        Index(
            "uq_transcode_jobs_active_video",
            "video_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'PROBING', 'PROCESSING', 'VERIFYING')"),
        ),
    )

    # id serves as the canonical generation ID for storage/videos/{video_id}/generations/{job.id}/
    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("videos.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[TranscodeJobStatus] = mapped_column(
        Enum(TranscodeJobStatus, name="transcode_job_status_enum", native_enum=True),
        default=TranscodeJobStatus.QUEUED,
        nullable=False,
        index=True,
    )
    priority: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    attempt: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        default=3,
        nullable=False,
    )
    worker_id: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )
    current_rendition: Mapped[Optional[str]] = mapped_column(
        String(16),
        nullable=True,
    )
    progress_percent: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False,
    )
    ffmpeg_fps: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    ffmpeg_speed: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    eta_seconds: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    error_code: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
    )
    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    video: Mapped["Video"] = relationship("Video", back_populates="transcode_jobs")

    configuration_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)



class VideoSubtitle(Base, TimestampMixin):
    __tablename__ = "video_subtitles"
    __table_args__ = (
        Index("ix_video_subtitles_video_lang", "video_id", "language_code"),
        Index(
            "uq_video_subtitles_one_default_per_video",
            "video_id",
            unique=True,
            postgresql_where=text("is_default = true"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("videos.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    language_code: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        index=True,
    )
    language_name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    format: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
    )
    playback_renderer: Mapped[str] = mapped_column(
        String(32),
        default="html5",
        server_default="html5",
        nullable=False,
    )
    generation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    font_asset_ids: Mapped[Optional[List[str]]] = mapped_column(
        JSON,
        default=list,
        nullable=True,
    )
    source_path: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )
    webvtt_path: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    is_forced: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    is_playable: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    video: Mapped["Video"] = relationship("Video", back_populates="subtitles")


class VideoFontAsset(Base, TimestampMixin):
    __tablename__ = "video_font_assets"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("videos.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    generation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    family_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )
    filename: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    relative_path: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )
    sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    size_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
    )
    mime_type: Mapped[str] = mapped_column(
        String(64),
        default="font/ttf",
        nullable=False,
    )

    video: Mapped["Video"] = relationship("Video", back_populates="font_assets")



class PlaybackSession(Base):
    __tablename__ = "playback_sessions"
    __table_args__ = (
        Index("ix_playback_sessions_video_started", "video_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("videos.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
    )
    ip_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    country_code: Mapped[Optional[str]] = mapped_column(
        String(2),
        nullable=True,
        index=True,
    )
    user_agent_short: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    duration_seconds: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False,
    )
    bytes_served: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
    )
    is_completed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    quality: Mapped[Optional[str]] = mapped_column(
        String(32),
        nullable=True,
        index=True,
    )
    device_type: Mapped[Optional[str]] = mapped_column(
        String(32),
        nullable=True,
    )
    referrer_domain: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True,
    )

    video: Mapped["Video"] = relationship("Video", back_populates="playback_sessions")
