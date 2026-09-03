from functools import lru_cache

from pydantic import Field
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
    access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    refresh_token_ttl_days: int = Field(default=30, ge=1, le=90)
    object_store_root: str = Field(default=".data/objects", min_length=1)
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, ge=1, le=250 * 1024 * 1024)
    redis_url: str = Field(default="redis://127.0.0.1:6379/0", min_length=1)
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
