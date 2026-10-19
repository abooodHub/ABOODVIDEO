"""Add trashed_at column for logical Trash and restore functionality.

Revision ID: 0011_trash_and_soft_delete
Revises: 0010_series_shares_policy
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0011_trash_and_soft_delete"
down_revision: Union[str, None] = "0010_series_shares_policy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("folders", sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_folders_trashed_at", "folders", ["trashed_at"])

    op.add_column("series", sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_series_trashed_at", "series", ["trashed_at"])

    op.add_column("seasons", sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_seasons_trashed_at", "seasons", ["trashed_at"])

    op.add_column("videos", sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_videos_trashed_at", "videos", ["trashed_at"])


def downgrade() -> None:
    op.drop_index("ix_videos_trashed_at", table_name="videos")
    op.drop_column("videos", "trashed_at")

    op.drop_index("ix_seasons_trashed_at", table_name="seasons")
    op.drop_column("seasons", "trashed_at")

    op.drop_index("ix_series_trashed_at", table_name="series")
    op.drop_column("series", "trashed_at")

    op.drop_index("ix_folders_trashed_at", table_name="folders")
    op.drop_column("folders", "trashed_at")
