"""Add immutable normalized-document artifact metadata."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_normalized_artifacts"
down_revision: str | None = "0004_job_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)

    op.create_table(
        "normalized_document_artifacts",
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
        sa.Column("parser_name", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("content_checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_object_key", sa.String(length=512), nullable=False),
        sa.Column("character_count", sa.Integer(), nullable=False),
        sa.Column("span_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "document_version_id",
            "parser_name",
            "parser_version",
            name="uq_normalized_artifact_version_parser",
        ),
        sa.UniqueConstraint(
            "artifact_object_key",
            name="uq_normalized_artifact_object_key",
        ),
    )
    op.create_index(
        "ix_normalized_document_artifacts_tenant_id",
        "normalized_document_artifacts",
        ["tenant_id"],
    )
    op.create_index(
        "ix_normalized_document_artifacts_document_version_id",
        "normalized_document_artifacts",
        ["document_version_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_normalized_document_artifacts_document_version_id",
        table_name="normalized_document_artifacts",
    )
    op.drop_index(
        "ix_normalized_document_artifacts_tenant_id",
        table_name="normalized_document_artifacts",
    )
    op.drop_table("normalized_document_artifacts")
