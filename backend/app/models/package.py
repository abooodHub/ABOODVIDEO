"""SQLAlchemy models for Pre-Encoded Studio Packages and Files."""
import enum
import uuid
from datetime import datetime
from typing import Optional, List, Any, Dict
from sqlalchemy import String, Integer, BigInteger, Text, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, utc_now


class PackageSessionStatus(str, enum.Enum):
    INITIALIZED = "INITIALIZED"
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    QUEUED_FOR_VERIFICATION = "QUEUED_FOR_VERIFICATION"
    VERIFYING = "VERIFYING"
    PROMOTING = "PROMOTING"
    READY = "READY"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


PackageStatus = PackageSessionStatus


class PackageFileStatus(str, enum.Enum):
    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class PackageUploadSession(Base):
    """Tracks a pre-encoded package upload session from ABOODVIDEO Studio."""

    __tablename__ = "package_upload_sessions"
    __table_args__ = (UniqueConstraint("created_by_key_id", "idempotency_key", name="uq_package_session_idempotency"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("videos.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_key_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("api_keys.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    request_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(30),
        default=PackageSessionStatus.INITIALIZED.value,
        nullable=False,
        index=True,
    )
    total_files: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    total_bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    completed_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
    )
    manifest_data: Mapped[Dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
    )
    staging_path: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
    )
    target_generation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    verification_job_id: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
        index=True,
    )
    verification_enqueued_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    verification_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    finalize_attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    # Relationships
    video = relationship("Video", backref="package_sessions", lazy="selectin")
    api_key = relationship("ApiKey", backref="package_sessions", lazy="selectin")
    files: Mapped[List["PackageFile"]] = relationship(
        "PackageFile",
        back_populates="package_session",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class PackageFile(Base):
    """Tracks an individual declared file within a pre-encoded package upload."""

    __tablename__ = "package_files"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    package_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("package_upload_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relative_path: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    expected_size: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    expected_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    media_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default=PackageFileStatus.PENDING.value,
        nullable=False,
        index=True,
    )
    received_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Unique constraint per session
    __table_args__ = (
        UniqueConstraint("package_session_id", "relative_path", name="uq_package_files_session_path"),
    )

    package_session: Mapped["PackageUploadSession"] = relationship(
        "PackageUploadSession",
        back_populates="files",
    )
