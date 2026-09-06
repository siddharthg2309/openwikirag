"""Persist W3C trace context on transactional outbox events."""

import sqlalchemy as sa
from alembic import op

revision = "0015_trace_context"
down_revision = "0014_memory_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbox_events", sa.Column("traceparent", sa.String(length=128)))


def downgrade() -> None:
    op.drop_column("outbox_events", "traceparent")
