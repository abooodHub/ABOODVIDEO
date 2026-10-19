"""Bind idempotency keys to the original package initialization request."""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0013_package_request_fingerprint"
down_revision: Union[str, None] = "0012_package_archive_idempotency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("package_upload_sessions", sa.Column("request_fingerprint", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("package_upload_sessions", "request_fingerprint")
