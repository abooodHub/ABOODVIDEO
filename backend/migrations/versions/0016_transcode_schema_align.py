"""Align video view-count storage with the declared 64-bit model.

Revision ID: 0016_transcode_schema_align
Revises: 0015_transcode_config_snapshot
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0016_transcode_schema_align"
down_revision: Union[str, None] = "0015_transcode_config_snapshot"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Widening INTEGER to BIGINT is lossless and avoids truncating any data.
    op.alter_column(
        "videos", "view_count",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )


def downgrade() -> None:
    # Narrowing can overflow if counts have grown beyond INTEGER range.
    # Deliberately refuse an unsafe downgrade rather than truncate data.
    raise RuntimeError("Downgrade refused: videos.view_count may exceed INTEGER range")
