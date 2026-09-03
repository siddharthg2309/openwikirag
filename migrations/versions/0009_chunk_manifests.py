"""Add immutable chunk-manifest metadata."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_chunk_manifests"
down_revision: str | None = "0008_scope_page_checksum"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)

    op.create_table(
        "chunk_manifest_artifacts",
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
        sa.Column("manifest_schema_version", sa.String(length=64), nullable=False),
        sa.Column("source_artifact_checksum", sa.String(length=64), nullable=False),
        sa.Column("chunking_config_checksum", sa.String(length=64), nullable=False),
        sa.Column("manifest_checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_object_key", sa.String(length=512), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("parent_count", sa.Integer(), nullable=False),
        sa.Column("child_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "document_version_id",
            "normalized_artifact_id",
            "manifest_schema_version",
            "chunking_config_checksum",
            name="uq_chunk_manifest_artifact_identity",
        ),
        sa.UniqueConstraint(
            "artifact_object_key",
            name="uq_chunk_manifest_artifact_object_key",
        ),
    )
    op.create_index(
        "ix_chunk_manifest_artifacts_tenant_id",
        "chunk_manifest_artifacts",
        ["tenant_id"],
    )
    op.create_index(
        "ix_chunk_manifest_artifacts_document_version_id",
        "chunk_manifest_artifacts",
        ["document_version_id"],
    )
    op.create_index(
        "ix_chunk_manifest_artifacts_normalized_artifact_id",
        "chunk_manifest_artifacts",
        ["normalized_artifact_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_chunk_manifest_artifacts_normalized_artifact_id",
        table_name="chunk_manifest_artifacts",
    )
    op.drop_index(
        "ix_chunk_manifest_artifacts_document_version_id",
        table_name="chunk_manifest_artifacts",
    )
    op.drop_index(
        "ix_chunk_manifest_artifacts_tenant_id",
        table_name="chunk_manifest_artifacts",
    )
    op.drop_table("chunk_manifest_artifacts")
