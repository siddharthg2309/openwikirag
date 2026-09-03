"""Add immutable WikiRAG generation artifact metadata."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_wiki_generation_artifacts"
down_revision: str | None = "0005_normalized_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)

    op.create_table(
        "wiki_generation_artifacts",
        sa.Column("id", uuid_type, primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            uuid_type,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_version_id",
            uuid_type,
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "normalized_artifact_id",
            uuid_type,
            sa.ForeignKey("normalized_document_artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("base_page_checksum", sa.String(length=64), nullable=False),
        sa.Column("source_artifact_checksum", sa.String(length=64), nullable=False),
        sa.Column("metadata_checksum", sa.String(length=64), nullable=False),
        sa.Column("prompt_checksum", sa.String(length=64), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("provider_identity", sa.String(length=255), nullable=False),
        sa.Column("generation_version", sa.String(length=64), nullable=False),
        sa.Column("result_checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_object_key", sa.String(length=512), nullable=False),
        sa.Column(
            "review_status",
            sa.String(length=16),
            server_default=sa.text("'draft'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "document_version_id",
            "normalized_artifact_id",
            "base_page_checksum",
            "generation_version",
            "prompt_checksum",
            "config_hash",
            "provider_identity",
            name="uq_wiki_generation_artifact_identity",
        ),
        sa.UniqueConstraint(
            "result_checksum_sha256",
            name="uq_wiki_generation_artifact_result_checksum",
        ),
        sa.UniqueConstraint(
            "artifact_object_key",
            name="uq_wiki_generation_artifact_object_key",
        ),
    )
    op.create_index(
        "ix_wiki_generation_artifacts_tenant_id",
        "wiki_generation_artifacts",
        ["tenant_id"],
    )
    op.create_index(
        "ix_wiki_generation_artifacts_document_version_id",
        "wiki_generation_artifacts",
        ["document_version_id"],
    )
    op.create_index(
        "ix_wiki_generation_artifacts_normalized_artifact_id",
        "wiki_generation_artifacts",
        ["normalized_artifact_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_wiki_generation_artifacts_normalized_artifact_id",
        table_name="wiki_generation_artifacts",
    )
    op.drop_index(
        "ix_wiki_generation_artifacts_document_version_id",
        table_name="wiki_generation_artifacts",
    )
    op.drop_index(
        "ix_wiki_generation_artifacts_tenant_id",
        table_name="wiki_generation_artifacts",
    )
    op.drop_table("wiki_generation_artifacts")
