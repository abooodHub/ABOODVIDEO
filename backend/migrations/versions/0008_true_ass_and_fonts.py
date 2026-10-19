"""Add true ASS subtitle metadata and video font assets.

Revision ID: 0008_true_ass_and_fonts
Revises: 0007_library_organization
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "0008_true_ass_and_fonts"
down_revision: Union[str, None] = "0007_library_organization"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Update video_subtitles table
    op.add_column(
        "video_subtitles",
        sa.Column("playback_renderer", sa.String(32), server_default="html5", nullable=False),
    )
    op.add_column(
        "video_subtitles",
        sa.Column("generation_id", PG_UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "video_subtitles",
        sa.Column("font_asset_ids", sa.JSON(), nullable=True),
    )
    op.create_index("ix_video_subtitles_generation_id", "video_subtitles", ["generation_id"])

    # 2. Create video_font_assets table
    op.create_table(
        "video_font_assets",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("video_id", PG_UUID(as_uuid=True), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("generation_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("family_name", sa.String(100), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("relative_path", sa.String(500), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), default=0, nullable=False),
        sa.Column("mime_type", sa.String(64), server_default="font/ttf", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )
    op.create_index("ix_video_font_assets_video_id", "video_font_assets", ["video_id"])
    op.create_index("ix_video_font_assets_generation_id", "video_font_assets", ["generation_id"])
    op.create_index("ix_video_font_assets_family_name", "video_font_assets", ["family_name"])


def downgrade() -> None:
    op.drop_index("ix_video_font_assets_family_name", table_name="video_font_assets")
    op.drop_index("ix_video_font_assets_generation_id", table_name="video_font_assets")
    op.drop_index("ix_video_font_assets_video_id", table_name="video_font_assets")
    op.drop_table("video_font_assets")

    op.drop_index("ix_video_subtitles_generation_id", table_name="video_subtitles")
    op.drop_column("video_subtitles", "font_asset_ids")
    op.drop_column("video_subtitles", "generation_id")
    op.drop_column("video_subtitles", "playback_renderer")
