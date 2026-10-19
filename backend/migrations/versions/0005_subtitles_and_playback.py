"""Subtitles and viewer playback sessions metadata migration.

Revision ID: 0005_subtitles_and_playback
Revises: 0004_transcoding_pipeline
Create Date: 2026-09-23 06:10:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005_subtitles_and_playback"
down_revision: Union[str, None] = "0004_transcoding_pipeline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create video_subtitles table
    op.create_table(
        "video_subtitles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "video_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("videos.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("language_code", sa.String(length=16), nullable=False, index=True),
        sa.Column("language_name", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("format", sa.String(length=16), nullable=False),
        sa.Column("source_path", sa.String(length=500), nullable=False),
        sa.Column("webvtt_path", sa.String(length=500), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_forced", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_playable", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_index(
        "ix_video_subtitles_video_lang",
        "video_subtitles",
        ["video_id", "language_code"],
    )

    # Partial unique index: at most one default subtitle per video
    op.create_index(
        "uq_video_subtitles_one_default_per_video",
        "video_subtitles",
        ["video_id"],
        unique=True,
        postgresql_where=sa.text("is_default = true"),
    )

    # 2. Create playback_sessions table
    op.create_table(
        "playback_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "video_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("videos.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("session_id", sa.String(length=64), unique=True, nullable=False, index=True),
        sa.Column("ip_hash", sa.String(length=64), nullable=False),
        sa.Column("user_agent_short", sa.String(length=128), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("duration_seconds", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("bytes_served", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("is_completed", sa.Boolean(), nullable=False, server_default="false"),
    )

    op.create_index(
        "ix_playback_sessions_video_started",
        "playback_sessions",
        ["video_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_playback_sessions_video_started", table_name="playback_sessions")
    op.drop_table("playback_sessions")

    op.drop_index("uq_video_subtitles_one_default_per_video", table_name="video_subtitles")
    op.drop_index("ix_video_subtitles_video_lang", table_name="video_subtitles")
    op.drop_table("video_subtitles")
