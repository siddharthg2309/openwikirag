"""Force transaction-local tenant RLS on derived-artifact metadata tables."""

from collections.abc import Sequence

from alembic import op

revision: str = "0017_derived_artifact_rls"
down_revision: str | None = "0016_document_intake_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DERIVED_ARTIFACT_TABLES = (
    "normalized_document_artifacts",
    "chunk_manifest_artifacts",
    "wiki_generation_artifacts",
    "wiki_page_artifacts",
    "knowledge_artifacts",
)


def upgrade() -> None:
    for table in _DERIVED_ARTIFACT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            """
        )


def downgrade() -> None:
    for table in reversed(_DERIVED_ARTIFACT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
