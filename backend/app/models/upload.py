"""Upload Session Model for Production Resumable Upload System."""
import enum
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from sqlalchemy import String, BigInteger, Integer, Enum, ForeignKey, DateTime
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base, TimestampMixin


class UploadSessionStatus(str, enum.Enum):
    INITIATED = "INITIATED"
    UPLOADING = "UPLOADING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class UploadSession(Base, TimestampMixin):
    __tablename__ = "upload_sessions"

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
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[UploadSessionStatus] = mapped_column(
        Enum(UploadSessionStatus, name="upload_session_status_enum", native_enum=True),
        default=UploadSessionStatus.INITIATED,
        nullable=False,
        index=True,
    )
    filename: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    file_size_bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    chunk_size_bytes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    total_chunks: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    received_chunks: Mapped[List[int]] = mapped_column(
        JSONB,
        default=list,
        nullable=False,
    )
    bytes_received: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
    )
    expected_sha256: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
    )
    final_sha256: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
    )
    mime_type: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )
    temp_directory: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )
    target_path: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    # Relationships
    video: Mapped["Video"] = relationship("Video", back_populates="upload_sessions")
    user: Mapped["User"] = relationship("User")
