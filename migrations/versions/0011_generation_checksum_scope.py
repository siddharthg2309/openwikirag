"""Content checksums do not establish tenant or document ownership."""

from alembic import op

revision = "0011_generation_checksum_scope"
down_revision = "0010_knowledge_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_wiki_generation_artifact_result_checksum", "wiki_generation_artifacts")
    op.create_unique_constraint(
        "uq_wiki_generation_artifact_owned_checksum",
        "wiki_generation_artifacts",
        ["tenant_id", "document_version_id", "result_checksum_sha256"],
    )


def downgrade() -> None:
    # The database must refuse this downgrade if cross-owner duplicates exist.
    op.create_unique_constraint(
        "uq_wiki_generation_artifact_result_checksum",
        "wiki_generation_artifacts",
        ["result_checksum_sha256"],
    )
    op.drop_constraint("uq_wiki_generation_artifact_owned_checksum", "wiki_generation_artifacts")
