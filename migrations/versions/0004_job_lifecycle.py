"""Add timestamps needed for leased ingestion-job lifecycle state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_job_lifecycle"
down_revision: str | None = "0003_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ingestion_jobs",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ingestion_jobs", "completed_at")
    op.drop_column("ingestion_jobs", "started_at")
