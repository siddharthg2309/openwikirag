"""Private conversations and immutable ordered messages."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_conversations"
down_revision = "0012_answer_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("title", sa.String(120)),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("next_sequence", sa.Integer(), nullable=False, server_default="1"),
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
        sa.CheckConstraint("status IN ('active','deleted')", name="ck_conversations_status"),
        sa.UniqueConstraint("id", "tenant_id", "user_id", name="uq_conversations_owner_identity"),
    )
    op.create_index(
        "ix_conversations_owner_updated", "conversations", ["tenant_id", "user_id", "updated_at"]
    )
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            nullable=False,
        ),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content_json", postgresql.JSONB(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("role IN ('user','assistant')", name="ck_conversation_messages_role"),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_conversation_message_sequence"),
        sa.ForeignKeyConstraint(
            ["conversation_id", "tenant_id", "user_id"],
            ["conversations.id", "conversations.tenant_id", "conversations.user_id"],
            name="fk_conversation_messages_owner",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_conversation_messages_owner",
        "conversation_messages",
        ["tenant_id", "user_id", "conversation_id"],
    )
    op.add_column("answer_runs", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_answer_runs_conversation_owner",
        "answer_runs",
        "conversations",
        ["conversation_id", "tenant_id", "user_id"],
        ["id", "tenant_id", "user_id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_answer_runs_conversation_id", "answer_runs", ["conversation_id"])


def downgrade() -> None:
    op.drop_column("answer_runs", "conversation_id")
    op.drop_table("conversation_messages")
    op.drop_table("conversations")
