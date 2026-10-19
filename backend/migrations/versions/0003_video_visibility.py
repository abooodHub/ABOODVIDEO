"""Video visibility policies and management metadata migration.

Revision ID: 0003_video_visibility
Revises: 0002_upload_sessions
Create Date: 2026-09-23 01:50:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003_video_visibility"
down_revision: Union[str, None] = "0002_upload_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create video_visibility_enum idempotently
    op.execute("""
    DO $$ BEGIN
        CREATE TYPE video_visibility_enum AS ENUM (
            'PRIVATE', 'UNLISTED', 'PUBLIC', 'PASSWORD_PROTECTED'
        );
    EXCEPTION
        WHEN duplicate_object THEN null;
    END $$;
    """)

    visibility_enum = postgresql.ENUM(
        "PRIVATE", "UNLISTED", "PUBLIC", "PASSWORD_PROTECTED",
        name="video_visibility_enum",
        create_type=False,
    )

    # 2. Add columns to videos table
    op.add_column("videos", sa.Column("visibility", visibility_enum, nullable=False, server_default="PRIVATE"))
    op.add_column("videos", sa.Column("share_id", sa.String(length=64), nullable=True))
    op.add_column("videos", sa.Column("share_revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("videos", sa.Column("viewer_password_hash", sa.String(length=255), nullable=True))
    op.add_column("videos", sa.Column("poster_path", sa.String(length=1024), nullable=True))
    op.add_column("videos", sa.Column("view_count", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("videos", sa.Column("bandwidth_bytes", sa.BigInteger(), nullable=False, server_default=sa.text("0")))

    # 3. Add index on share_id
    op.create_index("ix_videos_share_id", "videos", ["share_id"], unique=True)
    op.create_index("ix_videos_visibility", "videos", ["visibility"])


def downgrade() -> None:
    op.drop_index("ix_videos_visibility", table_name="videos")
    op.drop_index("ix_videos_share_id", table_name="videos")
    op.drop_column("videos", "bandwidth_bytes")
    op.drop_column("videos", "view_count")
    op.drop_column("videos", "poster_path")
    op.drop_column("videos", "viewer_password_hash")
    op.drop_column("videos", "share_revoked")
    op.drop_column("videos", "share_id")
    op.drop_column("videos", "visibility")
    op.execute("DROP TYPE IF EXISTS video_visibility_enum;")
