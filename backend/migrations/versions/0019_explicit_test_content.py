"""Introduce explicit test-content classification after the production lineage.

Existing rows remain visible: the non-null flag defaults to false. Local-only
0014 analytics/counter and 0015 country changes already exist in production
0014/0016/0018 and are deliberately not reapplied.
"""
from alembic import op
import sqlalchemy as sa

revision = "0019_explicit_test_content"
down_revision = "0018_playback_geo_country_policy"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("videos", sa.Column("is_test_content", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("videos", "is_test_content")
