"""Add metadata-only library organization fields.

Revision ID: 0007_library_organization
Revises: 0006_api_keys_and_packages
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007_library_organization"
down_revision: Union[str, None] = "0006_api_keys_and_packages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("videos", sa.Column("category", sa.String(100), nullable=True))
    op.add_column("videos", sa.Column("series_name", sa.String(150), nullable=True))
    op.add_column("videos", sa.Column("season_number", sa.Integer(), nullable=True))
    op.add_column("videos", sa.Column("episode_number", sa.Integer(), nullable=True))
    op.create_index("ix_videos_category", "videos", ["category"])
    op.create_index("ix_videos_series_name", "videos", ["series_name"])


def downgrade() -> None:
    op.drop_index("ix_videos_series_name", table_name="videos")
    op.drop_index("ix_videos_category", table_name="videos")
    op.drop_column("videos", "episode_number")
    op.drop_column("videos", "season_number")
    op.drop_column("videos", "series_name")
    op.drop_column("videos", "category")
