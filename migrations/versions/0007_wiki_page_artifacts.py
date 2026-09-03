"""Add self-contained WikiRAG page artifact metadata."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_wiki_page_artifacts"
down_revision: str | None = "0006_wiki_generation_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)

    op.create_table(
        "wiki_page_artifacts",
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
        sa.Column(
            "generation_artifact_id",
            uuid_type,
            sa.ForeignKey("wiki_generation_artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_checksum", sa.String(length=64), nullable=False),
        sa.Column(
            "generation_result_checksum_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("content_checksum_sha256", sa.String(length=64), nullable=False),
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
            "generation_artifact_id",
            "page_checksum",
            name="uq_wiki_page_artifact_identity",
        ),
        sa.UniqueConstraint(
            "content_checksum_sha256",
            name="uq_wiki_page_artifact_content_checksum",
        ),
        sa.UniqueConstraint(
            "artifact_object_key",
            name="uq_wiki_page_artifact_object_key",
        ),
    )
    op.create_index(
        "ix_wiki_page_artifacts_tenant_id",
        "wiki_page_artifacts",
        ["tenant_id"],
    )
    op.create_index(
        "ix_wiki_page_artifacts_document_version_id",
        "wiki_page_artifacts",
        ["document_version_id"],
    )
    op.create_index(
        "ix_wiki_page_artifacts_normalized_artifact_id",
        "wiki_page_artifacts",
        ["normalized_artifact_id"],
    )
    op.create_index(
        "ix_wiki_page_artifacts_generation_artifact_id",
        "wiki_page_artifacts",
        ["generation_artifact_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_wiki_page_artifacts_generation_artifact_id",
        table_name="wiki_page_artifacts",
    )
    op.drop_index(
        "ix_wiki_page_artifacts_normalized_artifact_id",
        table_name="wiki_page_artifacts",
    )
    op.drop_index(
        "ix_wiki_page_artifacts_document_version_id",
        table_name="wiki_page_artifacts",
    )
    op.drop_index(
        "ix_wiki_page_artifacts_tenant_id",
        table_name="wiki_page_artifacts",
    )
    op.drop_table("wiki_page_artifacts")
