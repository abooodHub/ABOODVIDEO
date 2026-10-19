"""Add series, seasons, shares tables and download policy fields.

Revision ID: 0010_series_shares_policy
Revises: 0009_folders_and_library_drive
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "0010_series_shares_policy"
down_revision: Union[str, None] = "0009_folders_and_library_drive"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Update folders with allow_download_policy
    op.add_column(
        "folders",
        sa.Column("allow_download_policy", sa.String(16), server_default="ALLOW", nullable=False),
    )

    # 2. Create series table
    op.create_table(
        "series",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_folder_id", PG_UUID(as_uuid=True), sa.ForeignKey("folders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("original_title", sa.String(255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("poster_path", sa.String(500), nullable=True),
        sa.Column("backdrop_path", sa.String(500), nullable=True),
        sa.Column("allow_download_policy", sa.String(16), server_default="INHERIT", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )
    op.create_index("ix_series_owner_id", "series", ["owner_id"])
    op.create_index("ix_series_parent_folder_id", "series", ["parent_folder_id"])
    op.create_index("ix_series_title", "series", ["title"])

    # 3. Create seasons table
    op.create_table(
        "seasons",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("series_id", PG_UUID(as_uuid=True), sa.ForeignKey("series.id", ondelete="CASCADE"), nullable=False),
        sa.Column("season_number", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("allow_download_policy", sa.String(16), server_default="INHERIT", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )
    op.create_index("ix_seasons_series_id", "seasons", ["series_id"])
    op.create_index("ix_seasons_season_number", "seasons", ["season_number"])

    # 4. Update videos with series_id, season_id, and allow_download_policy
    op.add_column(
        "videos",
        sa.Column("series_id", PG_UUID(as_uuid=True), sa.ForeignKey("series.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column(
        "videos",
        sa.Column("season_id", PG_UUID(as_uuid=True), sa.ForeignKey("seasons.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column(
        "videos",
        sa.Column("allow_download_policy", sa.String(16), server_default="INHERIT", nullable=False),
    )
    op.create_index("ix_videos_series_id", "videos", ["series_id"])
    op.create_index("ix_videos_season_id", "videos", ["season_id"])

    # 5. Create shares table
    op.create_table(
        "shares",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("permission", sa.String(32), server_default="VIEW_ONLY", nullable=False),
        sa.Column("allow_download", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )
    op.create_index("ix_shares_owner_id", "shares", ["owner_id"])
    op.create_index("ix_shares_target_type", "shares", ["target_type"])
    op.create_index("ix_shares_target_id", "shares", ["target_id"])
    op.create_index("ix_shares_token", "shares", ["token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_shares_token", table_name="shares")
    op.drop_index("ix_shares_target_id", table_name="shares")
    op.drop_index("ix_shares_target_type", table_name="shares")
    op.drop_index("ix_shares_owner_id", table_name="shares")
    op.drop_table("shares")

    op.drop_index("ix_videos_season_id", table_name="videos")
    op.drop_index("ix_videos_series_id", table_name="videos")
    op.drop_column("videos", "allow_download_policy")
    op.drop_column("videos", "season_id")
    op.drop_column("videos", "series_id")

    op.drop_index("ix_seasons_season_number", table_name="seasons")
    op.drop_index("ix_seasons_series_id", table_name="seasons")
    op.drop_table("seasons")

    op.drop_index("ix_series_title", table_name="series")
    op.drop_index("ix_series_parent_folder_id", table_name="series")
    op.drop_index("ix_series_owner_id", table_name="series")
    op.drop_table("series")

    op.drop_column("folders", "allow_download_policy")
