"""Production transcoding pipeline and adaptive HLS metadata migration.

Revision ID: 0004_transcoding_pipeline
Revises: 0003_video_visibility
Create Date: 2026-09-23 03:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004_transcoding_pipeline"
down_revision: Union[str, None] = "0003_video_visibility"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create transcode_job_status_enum idempotently
    op.execute("""
    DO $$ BEGIN
        CREATE TYPE transcode_job_status_enum AS ENUM (
            'QUEUED', 'PROBING', 'PROCESSING', 'VERIFYING', 'COMPLETED', 'FAILED', 'CANCELLED'
        );
    EXCEPTION
        WHEN duplicate_object THEN null;
    END $$;
    """)

    transcode_status_enum = postgresql.ENUM(
        "QUEUED", "PROBING", "PROCESSING", "VERIFYING", "COMPLETED", "FAILED", "CANCELLED",
        name="transcode_job_status_enum",
        create_type=False,
    )

    # 2. Create transcode_jobs table
    op.create_table(
        "transcode_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "video_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("videos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", transcode_status_enum, nullable=False, server_default="QUEUED"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("worker_id", sa.String(length=100), nullable=True),
        sa.Column("current_rendition", sa.String(length=16), nullable=True),
        sa.Column("progress_percent", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("ffmpeg_fps", sa.Float(), nullable=True),
        sa.Column("ffmpeg_speed", sa.Float(), nullable=True),
        sa.Column("eta_seconds", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("error_code", sa.String(length=50), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_index("ix_transcode_jobs_video_id", "transcode_jobs", ["video_id"])
    op.create_index("ix_transcode_jobs_status", "transcode_jobs", ["status"])

    # PostgreSQL Partial Unique Index: only 1 active transcode job per video
    op.create_index(
        "uq_transcode_jobs_active_video",
        "transcode_jobs",
        ["video_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'PROBING', 'PROCESSING', 'VERIFYING')"),
    )

    # 3. Expand video_renditions table
    op.add_column("video_renditions", sa.Column("video_codec", sa.String(length=32), nullable=False, server_default="h264"))
    op.add_column("video_renditions", sa.Column("audio_codec", sa.String(length=32), nullable=True))
    op.add_column("video_renditions", sa.Column("video_bitrate", sa.Integer(), nullable=True))
    op.add_column("video_renditions", sa.Column("audio_bitrate", sa.Integer(), nullable=True))
    op.add_column("video_renditions", sa.Column("playlist_path", sa.String(length=500), nullable=True))
    op.add_column("video_renditions", sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("video_renditions", sa.Column("is_ready", sa.Boolean(), nullable=False, server_default="false"))

    # Table-level unique constraint on (video_id, quality_label)
    op.create_unique_constraint(
        "uq_video_renditions_video_id_quality_label",
        "video_renditions",
        ["video_id", "quality_label"],
    )

    # 4. Expand videos table with generation tracking
    op.add_column("videos", sa.Column("active_generation_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("videos", sa.Column("master_playlist_path", sa.String(length=500), nullable=True))
    op.add_column("videos", sa.Column("transcode_progress_percent", sa.Float(), nullable=False, server_default="0.0"))


def downgrade() -> None:
    # 1. Revert videos columns
    op.drop_column("videos", "transcode_progress_percent")
    op.drop_column("videos", "master_playlist_path")
    op.drop_column("videos", "active_generation_id")

    # 2. Revert video_renditions unique constraint and columns
    op.drop_constraint("uq_video_renditions_video_id_quality_label", "video_renditions", type_="unique")
    op.drop_column("video_renditions", "is_ready")
    op.drop_column("video_renditions", "size_bytes")
    op.drop_column("video_renditions", "playlist_path")
    op.drop_column("video_renditions", "audio_bitrate")
    op.drop_column("video_renditions", "video_bitrate")
    op.drop_column("video_renditions", "audio_codec")
    op.drop_column("video_renditions", "video_codec")

    # 3. Drop transcode_jobs table and indexes
    op.drop_index("uq_transcode_jobs_active_video", table_name="transcode_jobs")
    op.drop_index("ix_transcode_jobs_status", table_name="transcode_jobs")
    op.drop_index("ix_transcode_jobs_video_id", table_name="transcode_jobs")
    op.drop_table("transcode_jobs")

    # 4. Drop transcode_job_status_enum
    op.execute("DROP TYPE IF EXISTS transcode_job_status_enum;")
