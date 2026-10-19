"""Add folders table and video folder association for video drive.

Revision ID: 0009_folders_and_library_drive
Revises: 0008_true_ass_and_fonts
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "0009_folders_and_library_drive"
down_revision: Union[str, None] = "0008_true_ass_and_fonts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create folders table
    op.create_table(
        "folders",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", PG_UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_id", PG_UUID(as_uuid=True), sa.ForeignKey("folders.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("folder_type", sa.String(32), server_default="folder", nullable=False),
        sa.Column("allow_download", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("share_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )
    op.create_index("ix_folders_user_id", "folders", ["user_id"])
    op.create_index("ix_folders_parent_id", "folders", ["parent_id"])
    op.create_index("ix_folders_name", "folders", ["name"])
    op.create_index("ix_folders_share_id", "folders", ["share_id"], unique=True)

    # 2. Add folder_id and allow_download to videos table
    op.add_column(
        "videos",
        sa.Column("folder_id", PG_UUID(as_uuid=True), sa.ForeignKey("folders.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column(
        "videos",
        sa.Column("allow_download", sa.Boolean(), server_default="true", nullable=False),
    )
    op.create_index("ix_videos_folder_id", "videos", ["folder_id"])


def downgrade() -> None:
    op.drop_index("ix_videos_folder_id", table_name="videos")
    op.drop_column("videos", "allow_download")
    op.drop_column("videos", "folder_id")

    op.drop_index("ix_folders_share_id", table_name="folders")
    op.drop_index("ix_folders_name", table_name="folders")
    op.drop_index("ix_folders_parent_id", table_name="folders")
    op.drop_index("ix_folders_user_id", table_name="folders")
    op.drop_table("folders")
