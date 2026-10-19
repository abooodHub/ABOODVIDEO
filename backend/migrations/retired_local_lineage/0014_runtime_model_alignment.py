"""Align the runtime ORM with the schema through revision 0013.

Revision ID: 0014_runtime_model_alignment
Revises: 0013_package_request_fingerprint
Create Date: 2026-10-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0014_runtime_model_alignment"
down_revision: Union[str, None] = "0013_package_request_fingerprint"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add playback analytics fields and widen the monotonic view counter."""
    op.add_column(
        "playback_sessions",
        sa.Column("quality", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "playback_sessions",
        sa.Column("device_type", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "playback_sessions",
        sa.Column("referrer_domain", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_playback_sessions_quality", "playback_sessions", ["quality"]
    )
    op.create_index(
        "ix_playback_sessions_referrer_domain", "playback_sessions", ["referrer_domain"]
    )
    op.alter_column(
        "videos",
        "view_count",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
        postgresql_using="view_count::bigint",
    )


def downgrade() -> None:
    """Remove analytics fields and narrow the counter only if values fit."""
    bind = op.get_bind()
    has_out_of_range_counts = bind.execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM videos "
            "WHERE view_count < 0 OR view_count > 2147483647"
            ")"
        )
    ).scalar()
    if has_out_of_range_counts:
        raise RuntimeError(
            "Cannot downgrade view_count to INTEGER while values exceed its range."
        )

    op.alter_column(
        "videos",
        "view_count",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
        postgresql_using="view_count::integer",
    )
    op.drop_index(
        "ix_playback_sessions_referrer_domain", table_name="playback_sessions"
    )
    op.drop_index("ix_playback_sessions_quality", table_name="playback_sessions")
    op.drop_column("playback_sessions", "referrer_domain")
    op.drop_column("playback_sessions", "device_type")
    op.drop_column("playback_sessions", "quality")
