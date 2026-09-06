"""Compose one dense embedding identity across application processes."""

from openwikirag.application.embeddings import (
    DenseEmbeddingProvider,
    DeterministicHashEmbeddingProvider,
    EmbeddingConfig,
)
from openwikirag.core.config import Settings

from .ollama_embeddings import OllamaDenseEmbeddingProvider


def build_dense_embedding_config(settings: Settings) -> EmbeddingConfig:
    """Build the worker/API-compatible dense configuration from settings."""

    if not settings.dense_embedding_model.strip():
        return EmbeddingConfig()
    return EmbeddingConfig(
        model_identity=(
            f"{settings.dense_embedding_model.strip()}"
            f"@{settings.dense_embedding_model_digest}"
        ),
        dimensions=settings.dense_embedding_dimensions,
    )


def build_dense_embedding_provider(settings: Settings) -> DenseEmbeddingProvider:
    """Select the semantic provider only for complete server-owned identity."""

    if not settings.dense_embedding_model.strip():
        return DeterministicHashEmbeddingProvider()
    return OllamaDenseEmbeddingProvider(
        model=settings.dense_embedding_model,
        digest=settings.dense_embedding_model_digest,
        base_url=settings.dense_embedding_base_url,
        timeout_seconds=settings.dense_embedding_timeout_seconds,
    )


__all__ = ["build_dense_embedding_config", "build_dense_embedding_provider"]
