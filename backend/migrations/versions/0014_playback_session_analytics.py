"""Add persisted playback session dimensions required by analytics."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0014_playback_session_analytics"
down_revision: Union[str, None] = "0013_package_request_fingerprint"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("playback_sessions", sa.Column("quality", sa.String(length=32), nullable=True))
    op.add_column("playback_sessions", sa.Column("device_type", sa.String(length=32), nullable=True))
    op.add_column("playback_sessions", sa.Column("referrer_domain", sa.String(length=255), nullable=True))
    op.create_index("ix_playback_sessions_quality", "playback_sessions", ["quality"])
    op.create_index("ix_playback_sessions_referrer_domain", "playback_sessions", ["referrer_domain"])


def downgrade() -> None:
    op.drop_index("ix_playback_sessions_referrer_domain", table_name="playback_sessions")
    op.drop_index("ix_playback_sessions_quality", table_name="playback_sessions")
    op.drop_column("playback_sessions", "referrer_domain")
    op.drop_column("playback_sessions", "device_type")
    op.drop_column("playback_sessions", "quality")
