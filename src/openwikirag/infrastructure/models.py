from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import Uuid


class Base(DeclarativeBase):
    """Base for persistence models owned by the infrastructure layer."""


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default=text("'active'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    auth_provider_subject: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )
    password_hash: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_memberships_tenant_user"),
        CheckConstraint(
            "role IN ('viewer', 'editor', 'admin', 'operator')",
            name="ck_memberships_role",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class RefreshToken(Base):
    """Rotatable opaque refresh-token record; only its hash is persisted."""

    __tablename__ = "refresh_tokens"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("refresh_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class AuditEvent(Base):
    """Append-only security and lifecycle event belonging to a tenant when known."""

    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict[str, object] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class Document(Base):
    """Logical document identity owned by one tenant."""

    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    current_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
    )


class DocumentVersion(Base):
    """Immutable source revision and its canonical raw object reference."""

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "version_number",
            name="uq_document_versions_document_number",
        ),
        UniqueConstraint("source_object_key", name="uq_document_versions_object_key"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    sanitized_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="uploaded", server_default=text("'uploaded'")
    )
    pipeline_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="ingestion-v1", server_default=text("'ingestion-v1'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class NormalizedDocumentArtifact(Base):
    """Immutable normalized text/provenance artifact for one source parser version."""

    __tablename__ = "normalized_document_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id",
            "parser_name",
            "parser_version",
            name="uq_normalized_artifact_version_parser",
        ),
        UniqueConstraint(
            "artifact_object_key",
            name="uq_normalized_artifact_object_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parser_name: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    content_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    character_count: Mapped[int] = mapped_column(nullable=False)
    span_count: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class WikiGenerationArtifact(Base):
    """Immutable metadata index for one validated generated WikiRAG payload."""

    __tablename__ = "wiki_generation_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id",
            "normalized_artifact_id",
            "base_page_checksum",
            "generation_version",
            "prompt_checksum",
            "config_hash",
            "provider_identity",
            name="uq_wiki_generation_artifact_identity",
        ),
        UniqueConstraint(
            "result_checksum_sha256",
            name="uq_wiki_generation_artifact_result_checksum",
        ),
        UniqueConstraint(
            "artifact_object_key",
            name="uq_wiki_generation_artifact_object_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    normalized_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("normalized_document_artifacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    base_page_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    source_artifact_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    generation_version: Mapped[str] = mapped_column(String(64), nullable=False)
    result_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default=text("'draft'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class WikiPageArtifact(Base):
    """Immutable metadata index for a self-contained generated WikiRAG page."""

    __tablename__ = "wiki_page_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "generation_artifact_id",
            "page_checksum",
            name="uq_wiki_page_artifact_identity",
        ),
        UniqueConstraint(
            "tenant_id",
            "content_checksum_sha256",
            name="uq_wiki_page_artifact_tenant_content_checksum",
        ),
        UniqueConstraint(
            "artifact_object_key",
            name="uq_wiki_page_artifact_object_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    normalized_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("normalized_document_artifacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    generation_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("wiki_generation_artifacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    page_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_result_checksum_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    content_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default=text("'draft'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class IngestionJob(Base):
    """Durable pending work record; delivery and lease handling come later."""

    __tablename__ = "ingestion_jobs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    job_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ingestion", server_default=text("'ingestion'")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(nullable=False, default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(
        nullable=False, default=3, server_default=text("3")
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
    )


class OutboxEvent(Base):
    """Committed database event waiting for delivery to a derived stream."""

    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_events_pending", "published_at", "occurred_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publish_attempts: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
