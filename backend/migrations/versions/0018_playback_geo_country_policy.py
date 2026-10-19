"""Store Cloudflare country codes for privacy-safe playback geography."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0018_playback_geo_country_policy"
down_revision: Union[str, None] = "0017_platform_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("playback_sessions", sa.Column("country_code", sa.String(length=2), nullable=True))
    op.create_index("ix_playback_sessions_country_code", "playback_sessions", ["country_code"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_playback_sessions_country_code", table_name="playback_sessions")
    op.drop_column("playback_sessions", "country_code")
