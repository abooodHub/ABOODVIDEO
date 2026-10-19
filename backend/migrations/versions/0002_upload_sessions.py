"""Upload sessions and video metadata expansion migration.

Revision ID: 0002_upload_sessions
Revises: 0001_initial_schema
Create Date: 2026-09-22 23:45:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_upload_sessions"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create upload_session_status_enum idempotently
    op.execute("""
    DO $$ BEGIN
        CREATE TYPE upload_session_status_enum AS ENUM (
            'INITIATED', 'UPLOADING', 'PAUSED', 'COMPLETED', 'CANCELLED', 'EXPIRED'
        );
    EXCEPTION
        WHEN duplicate_object THEN null;
    END $$;
    """)

    upload_status_type = postgresql.ENUM(
        "INITIATED", "UPLOADING", "PAUSED", "COMPLETED", "CANCELLED", "EXPIRED",
        name="upload_session_status_enum",
        create_type=False,
    )

    # 2. Expand videos table with metadata columns
    op.add_column("videos", sa.Column("source_duration_seconds", sa.Float(), nullable=True))
    op.add_column("videos", sa.Column("source_resolution_width", sa.Integer(), nullable=True))
    op.add_column("videos", sa.Column("source_resolution_height", sa.Integer(), nullable=True))
    op.add_column("videos", sa.Column("source_bitrate", sa.BigInteger(), nullable=True))
    op.add_column("videos", sa.Column("source_codec", sa.String(64), nullable=True))
    op.add_column("videos", sa.Column("source_path", sa.String(500), nullable=True))
    op.add_column("videos", sa.Column("upload_progress_percent", sa.Float(), server_default="0.0", nullable=False))

    # 3. Create upload_sessions table
    op.create_table(
        "upload_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("video_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", upload_status_type, nullable=False, server_default="INITIATED"),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("chunk_size_bytes", sa.Integer(), nullable=False),
        sa.Column("total_chunks", sa.Integer(), nullable=False),
        sa.Column("received_chunks", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("bytes_received", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("expected_sha256", sa.String(64), nullable=True),
        sa.Column("final_sha256", sa.String(64), nullable=True),
        sa.Column("mime_type", sa.String(100), nullable=True),
        sa.Column("temp_directory", sa.String(500), nullable=False),
        sa.Column("target_path", sa.String(500), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_upload_sessions_video_id", "upload_sessions", ["video_id"])
    op.create_index("ix_upload_sessions_user_id", "upload_sessions", ["user_id"])
    op.create_index("ix_upload_sessions_status", "upload_sessions", ["status"])
    op.create_index("ix_upload_sessions_expires_at", "upload_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_table("upload_sessions")
    op.drop_column("videos", "upload_progress_percent")
    op.drop_column("videos", "source_path")
    op.drop_column("videos", "source_codec")
    op.drop_column("videos", "source_bitrate")
    op.drop_column("videos", "source_resolution_height")
    op.drop_column("videos", "source_resolution_width")
    op.drop_column("videos", "source_duration_seconds")
    op.execute("DROP TYPE IF EXISTS upload_session_status_enum")
