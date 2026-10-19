"""Add authentication timestamp and audit request correlation.

Revision ID: 0017_platform_foundation
Revises: 0016_transcode_schema_align
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0017_platform_foundation"
down_revision: Union[str, None] = "0016_transcode_schema_align"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("last_authenticated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("audit_logs", sa.Column("request_id", sa.String(length=64), nullable=True))
    op.create_index("ix_audit_logs_request_id", "audit_logs", ["request_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_audit_logs_request_id", table_name="audit_logs")
    op.drop_column("audit_logs", "request_id")
    op.drop_column("users", "last_authenticated_at")
