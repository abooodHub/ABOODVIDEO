"""Series and Season Models for Media Library Organization."""
import uuid
from datetime import datetime
from typing import Optional, List, TYPE_CHECKING
from sqlalchemy import (
    String,
    Text,
    Integer,
    ForeignKey,
    DateTime,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.folder import Folder
    from app.models.video import Video


class Series(Base, TimestampMixin):
    __tablename__ = "series"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_folder_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("folders.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )
    original_title: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    year: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )
    poster_path: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
    )
    backdrop_path: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
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
    owner: Mapped["User"] = relationship("User")
    parent_folder: Mapped[Optional["Folder"]] = relationship("Folder", back_populates="series_list")
    seasons: Mapped[List["Season"]] = relationship(
        "Season",
        back_populates="series",
        cascade="all, delete-orphan",
        order_by="Season.sort_order",
    )
    videos: Mapped[List["Video"]] = relationship(
        "Video",
        back_populates="series",
    )


class Season(Base, TimestampMixin):
    __tablename__ = "seasons"
    __table_args__ = (
        Index("ix_seasons_season_number", "season_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    series_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("series.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    season_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    sort_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
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
    series: Mapped["Series"] = relationship("Series", back_populates="seasons")
    episodes: Mapped[List["Video"]] = relationship(
        "Video",
        back_populates="season",
        order_by="Video.episode_number",
    )
