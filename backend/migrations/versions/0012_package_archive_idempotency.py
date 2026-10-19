"""Add idempotent Studio package initialization."""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0012_package_archive_idempotency"
down_revision: Union[str, None] = "0011_trash_and_soft_delete"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("package_upload_sessions", sa.Column("idempotency_key", sa.String(length=64), nullable=True))
    op.create_unique_constraint(
        "uq_package_session_idempotency",
        "package_upload_sessions",
        ["created_by_key_id", "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_package_session_idempotency", "package_upload_sessions", type_="unique")
    op.drop_column("package_upload_sessions", "idempotency_key")
