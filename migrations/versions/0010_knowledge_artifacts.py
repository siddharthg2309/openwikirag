"""Canonical evidence-backed graph artifacts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_knowledge_artifacts"
down_revision = "0009_chunk_manifests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "document_version_id",
            sa.Uuid(),
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "manifest_id",
            sa.Uuid(),
            sa.ForeignKey("chunk_manifest_artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("extractor", sa.String(64), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "manifest_id", "extractor", name="uq_knowledge_manifest_extractor"
        ),
    )
    op.create_index("ix_knowledge_artifacts_tenant_id", "knowledge_artifacts", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("knowledge_artifacts")
