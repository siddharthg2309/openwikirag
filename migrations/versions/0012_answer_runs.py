"""Owner-private durable answer runs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_answer_runs"
down_revision = "0011_generation_checksum_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "answer_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("request_json", postgresql.JSONB(), nullable=False),
        sa.Column("provider_identity", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("answer_json", postgresql.JSONB(), nullable=True),
        sa.Column("answer_checksum", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','retryable','complete','failed')",
            name="ck_answer_runs_status",
        ),
        sa.CheckConstraint("attempts >= 0 AND attempts <= 5", name="ck_answer_runs_attempts"),
        sa.CheckConstraint(
            "(status = 'complete') = (answer_json IS NOT NULL AND answer_checksum IS NOT NULL)",
            name="ck_answer_runs_result",
        ),
    )
    op.create_index("ix_answer_runs_owner", "answer_runs", ["tenant_id", "user_id", "created_at"])


def downgrade() -> None:
    op.drop_table("answer_runs")
