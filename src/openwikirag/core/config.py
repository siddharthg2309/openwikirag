from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed configuration loaded from environment variables.

    The ``OPENWIKIRAG_`` prefix keeps application settings separate from
    dependency settings consumed directly by Docker Compose.
    """

    model_config = SettingsConfigDict(
        env_prefix="OPENWIKIRAG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = Field(default="development", min_length=1)
    log_level: str = Field(default="INFO", min_length=1)
    api_host: str = Field(default="127.0.0.1", min_length=1)
    api_port: int = Field(default=8000, ge=1, le=65535)
    worker_health_host: str = Field(default="127.0.0.1", min_length=1)
    worker_health_port: int = Field(default=8001, ge=1, le=65535)
    database_url: str = Field(
        default="postgresql+asyncpg://openwikirag_app:openwikirag-dev-password@127.0.0.1:5432/openwikirag",
        min_length=1,
    )
    migration_database_url: str = Field(
        default="postgresql+asyncpg://openwikirag:openwikirag-dev-password@127.0.0.1:5432/openwikirag",
        min_length=1,
    )
    jwt_secret: str = Field(
        default="openwikirag-development-secret-change-me-now",
        min_length=32,
    )
    jwt_issuer: str = Field(default="openwikirag-local", min_length=1)
    jwt_audience: str = Field(default="openwikirag-api", min_length=1)
    auth_mode: Literal["local", "oidc"] = "local"
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    oidc_algorithms: str = Field(default="RS256", min_length=1)
    oidc_tenant_claim: str = Field(default="tenant_id", min_length=1, max_length=128)
    oidc_jwks_cache_seconds: int = Field(default=300, ge=30, le=86_400)
    oidc_jwks_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    refresh_token_ttl_days: int = Field(default=30, ge=1, le=90)
    object_store_root: str = Field(default=".data/objects", min_length=1)
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, ge=1, le=250 * 1024 * 1024)
    max_request_bytes: int = Field(default=32 * 1024 * 1024, ge=1, le=300 * 1024 * 1024)
    auth_rate_limit_enabled: bool = False
    auth_rate_limit_requests: int = Field(default=10, ge=1, le=10_000)
    auth_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3_600)
    api_rate_limit_enabled: bool = False
    api_rate_limit_requests: int = Field(default=60, ge=1, le=100_000)
    api_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3_600)
    trusted_proxy_cidrs: str = ""
    redis_url: str = Field(default="redis://127.0.0.1:6379/0", min_length=1)
    qdrant_url: str = Field(default="http://127.0.0.1:6333", min_length=1)
    qdrant_api_key: str | None = None
    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "openwikirag-dev-password"
    neo4j_database: str = "neo4j"
    graph_enabled: bool = False
    reranker_model: str = Field(default="", max_length=255)
    reranker_revision: str = Field(default="", pattern=r"^(|[0-9a-f]{40})$")
    answer_model: str = Field(default="", max_length=255)
    answer_model_digest: str = Field(default="", pattern=r"^(|[0-9a-f]{64})$")
    answer_base_url: str = Field(default="http://127.0.0.1:11434", min_length=1)
    wiki_generation_model: str = Field(default="", max_length=255)
    wiki_generation_model_digest: str = Field(default="", pattern=r"^(|[0-9a-f]{64})$")
    wiki_generation_base_url: str = Field(default="http://127.0.0.1:11434", min_length=1)
    wiki_generation_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    dense_embedding_model: str = Field(default="", max_length=255)
    dense_embedding_model_digest: str = Field(default="", pattern=r"^(|[0-9a-f]{64})$")
    dense_embedding_base_url: str = Field(default="http://127.0.0.1:11434", min_length=1)
    dense_embedding_dimensions: int = Field(default=128, ge=1, le=8192)
    dense_embedding_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    score_cache_enabled: bool = False
    retrieval_index_version: str = Field(
        default="hash-dense-sparse-v1", min_length=1, max_length=255
    )

    @model_validator(mode="after")
    def validate_reranker(self) -> Self:
        if self.max_request_bytes < self.max_upload_bytes:
            raise ValueError("The HTTP request bound must cover the decoded upload bound.")
        if bool(self.reranker_model.strip()) != bool(self.reranker_revision):
            raise ValueError("Reranker model and immutable revision must be configured together.")
        if bool(self.answer_model.strip()) != bool(self.answer_model_digest):
            raise ValueError("Answer model and expected digest must be configured together.")
        if bool(self.wiki_generation_model.strip()) != bool(self.wiki_generation_model_digest):
            raise ValueError(
                "Wiki generation model and expected digest must be configured together."
            )
        if bool(self.dense_embedding_model.strip()) != bool(self.dense_embedding_model_digest):
            raise ValueError(
                "Dense embedding model and expected digest must be configured together."
            )
        try:
            _ = self.trusted_proxy_networks
        except ValueError as exc:
            raise ValueError("Trusted proxy CIDRs must be valid IP networks.") from exc
        algorithms = self.oidc_algorithm_list
        supported = {
            "RS256",
            "RS384",
            "RS512",
            "PS256",
            "PS384",
            "PS512",
            "ES256",
            "ES384",
            "ES512",
            "EdDSA",
        }
        if self.auth_mode == "oidc":
            if not self.oidc_issuer.strip() or not self.oidc_audience.strip():
                raise ValueError("OIDC issuer and audience are required in OIDC mode.")
            if not self.oidc_jwks_url.strip():
                raise ValueError("OIDC JWKS URL is required in OIDC mode.")
            if not algorithms or any(item not in supported for item in algorithms):
                raise ValueError("OIDC algorithms must be configured asymmetric JWT algorithms.")
            if (
                self.environment.casefold() in {"production", "prod"}
                and not self.oidc_jwks_url.startswith("https://")
            ):
                raise ValueError("Production OIDC JWKS URL must use HTTPS.")
        elif self.environment.casefold() in {"production", "prod"}:
            raise ValueError("Production authentication requires OIDC mode.")
        return self

    @property
    def oidc_algorithm_list(self) -> tuple[str, ...]:
        """Return normalized OIDC algorithms from the comma-separated setting."""

        return tuple(item.strip() for item in self.oidc_algorithms.split(",") if item.strip())

    @property
    def trusted_proxy_networks(self) -> tuple[IPv4Network | IPv6Network, ...]:
        """Return explicitly configured reverse-proxy networks."""

        raw_values = self.trusted_proxy_cidrs.strip()
        if not raw_values:
            return ()
        values = tuple(item.strip() for item in raw_values.split(","))
        if any(not item for item in values):
            raise ValueError("Trusted proxy CIDRs cannot contain empty entries.")
        return tuple(ip_network(item, strict=False) for item in values)

    ingestion_stream_name: str = Field(default="openwikirag:ingestion", min_length=1)
    outbox_batch_size: int = Field(default=100, ge=1, le=1000)
    ingestion_consumer_group: str = Field(default="openwikirag-ingestion", min_length=1)
    ingestion_consumer_name: str = Field(default="local-worker", min_length=1)
    job_lease_seconds: int = Field(default=60, ge=1, le=3600)
    job_retry_backoff_base_seconds: int = Field(default=5, ge=1, le=3600)
    job_retry_backoff_max_seconds: int = Field(default=300, ge=1, le=86400)
    dead_letter_stream_name: str = Field(
        default="openwikirag:ingestion:dead-letter",
        min_length=1,
    )
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    worker_error_backoff_seconds: float = Field(default=5.0, gt=0, le=300)
    ocr_enabled: bool = False
    ocr_dpi: int = Field(default=200, ge=72, le=600)
    ocr_language: str = Field(default="eng", min_length=1, max_length=64)
    ocr_page_segmentation_mode: int = Field(default=6, ge=0, le=13)
    ocr_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    ocr_max_pages: int = Field(default=50, ge=1, le=500)
    wiki_generation_config_hash: str = Field(
        default="0" * 64,
        pattern=r"^[0-9a-f]{64}$",
    )
    wiki_regeneration_config_hash: str = Field(
        default="1" * 64,
        pattern=r"^[0-9a-f]{64}$",
    )


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings object per process."""

    return Settings()
