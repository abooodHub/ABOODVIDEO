"""Explicit test-content flag; legitimate titles are never classified by text."""
from alembic import op
import sqlalchemy as sa

revision = "0016_explicit_test_content"
down_revision = "0015_playback_geo_country_policy"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("videos", sa.Column("is_test_content", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("videos", "is_test_content")
