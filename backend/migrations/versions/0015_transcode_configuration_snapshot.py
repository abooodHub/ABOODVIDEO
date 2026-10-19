"""Persist immutable transcode configuration per job.

Revision ID: 0015_transcode_config_snapshot
Revises: 0014_playback_session_analytics
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0015_transcode_config_snapshot"
down_revision: Union[str, None] = "0014_playback_session_analytics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("transcode_jobs", sa.Column("configuration_snapshot", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("transcode_jobs", "configuration_snapshot")
