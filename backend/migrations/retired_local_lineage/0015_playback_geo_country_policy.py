"""Store country-level playback analytics without raw IP addresses.

Revision ID: 0015_playback_geo_country_policy
Revises: 0014_runtime_model_alignment
Create Date: 2026-10-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0015_playback_geo_country_policy"
down_revision: Union[str, None] = "0014_runtime_model_alignment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("playback_sessions", sa.Column("country_code", sa.String(length=2), nullable=True))
    op.create_index("ix_playback_sessions_country_code", "playback_sessions", ["country_code"])


def downgrade() -> None:
    op.drop_index("ix_playback_sessions_country_code", table_name="playback_sessions")
    op.drop_column("playback_sessions", "country_code")
