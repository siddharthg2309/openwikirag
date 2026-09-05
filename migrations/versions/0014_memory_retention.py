"""Bounded summaries, explicit memory and deletion tombstones."""

import sqlalchemy as sa
from alembic import op

revision = "0014_memory_retention"
down_revision = "0013_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("answer_runs", sa.Column("history_context", sa.String(2000)))
    op.add_column("answer_runs", sa.Column("history_checksum", sa.String(64)))
    op.add_column("answer_runs", sa.Column("memory_fingerprint", sa.String(64)))
    op.add_column("answer_runs", sa.Column("context_purge_after", sa.DateTime(timezone=True)))
    for column in (
        sa.Column("summary_text", sa.String(2000)),
        sa.Column("summary_checksum", sa.String(64)),
        sa.Column("summary_through_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("purge_after", sa.DateTime(timezone=True)),
    ):
        op.add_column("conversations", column)
    op.create_check_constraint(
        "ck_conversations_deletion",
        "conversations",
        "(status = 'deleted') = (deleted_at IS NOT NULL AND purge_after IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_conversations_summary",
        "conversations",
        "(summary_text IS NULL) = (summary_checksum IS NULL)",
    )
    op.create_check_constraint(
        "ck_conversations_summary_sequence",
        "conversations",
        "summary_through_sequence >= 0",
    )
    op.create_table(
        "user_memories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("memory_key", sa.String(64), nullable=False),
        sa.Column("memory_value", sa.String(500), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("purge_after", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "user_id", "memory_key", name="uq_user_memory_key"),
        sa.CheckConstraint(
            "(deleted_at IS NULL) = (purge_after IS NULL)", name="ck_user_memory_deletion"
        ),
    )
    op.create_index("ix_user_memories_owner", "user_memories", ["tenant_id", "user_id"])


def downgrade() -> None:
    op.drop_table("user_memories")
    op.drop_constraint("ck_conversations_summary_sequence", "conversations", type_="check")
    op.drop_constraint("ck_conversations_summary", "conversations", type_="check")
    op.drop_constraint("ck_conversations_deletion", "conversations", type_="check")
    for name in (
        "purge_after",
        "deleted_at",
        "summary_through_sequence",
        "summary_checksum",
        "summary_text",
    ):
        op.drop_column("conversations", name)
    op.drop_column("answer_runs", "history_context")
    op.drop_column("answer_runs", "history_checksum")
    op.drop_column("answer_runs", "memory_fingerprint")
    op.drop_column("answer_runs", "context_purge_after")
