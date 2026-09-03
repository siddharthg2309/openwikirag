"""Scope page-artifact content uniqueness to the owning tenant."""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_scope_page_checksum"
down_revision: str | None = "0007_wiki_page_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_wiki_page_artifact_content_checksum",
        "wiki_page_artifacts",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_wiki_page_artifact_tenant_content_checksum",
        "wiki_page_artifacts",
        ["tenant_id", "content_checksum_sha256"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_wiki_page_artifact_tenant_content_checksum",
        "wiki_page_artifacts",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_wiki_page_artifact_content_checksum",
        "wiki_page_artifacts",
        ["content_checksum_sha256"],
    )
