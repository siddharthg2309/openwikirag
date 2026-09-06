"""Run a redacted semantic-embedding sanity smoke against pinned Ollama."""

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from openwikirag.application.embeddings import (
    DenseEmbedding,
    DenseEmbeddingProvider,
    EmbeddingConfig,
    EmbeddingError,
    EmbeddingRequest,
)
from openwikirag.core.config import Settings
from openwikirag.infrastructure.ollama_embeddings import OllamaDenseEmbeddingProvider

DEFAULT_MINIMUM_MARGIN: Final[float] = 0.01


@dataclass(frozen=True, slots=True)
class EmbeddingFixture:
    """One small comparison with a related and a distractor passage."""

    label: str
    query: str
    related: str
    distractor: str


FIXTURES: tuple[EmbeddingFixture, ...] = (
    EmbeddingFixture(
        "tenant_isolation",
        "PostgreSQL row-level security prevents one tenant from reading another "
        "tenant's documents.",
        "The database enforces organization boundaries so data from one customer "
        "is hidden from other customers.",
        "A JWT access token authenticates a user before the API checks their permissions.",
    ),
    EmbeddingFixture(
        "object_storage",
        "Raw document bytes are stored in object storage, while PostgreSQL stores "
        "immutable metadata and checksums.",
        "The file itself lives in blob storage and the relational database keeps "
        "its metadata and integrity hash.",
        "The API uses a short-lived access token and refresh-token rotation for authentication.",
    ),
    EmbeddingFixture(
        "durable_worker",
        "Redis Streams delivers ingestion jobs at least once, so workers must "
        "make processing idempotent.",
        "An asynchronous queue may redeliver a job, therefore repeated processing must be safe.",
        "Neo4j stores relationships between entities extracted from knowledge documents.",
    ),
)


class EmbeddingSmokeError(EmbeddingError):
    """Raised when a live embedding smoke result fails its bounded checks."""


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _request(text: str, config: EmbeddingConfig) -> EmbeddingRequest:
    return EmbeddingRequest(
        text=text,
        input_checksum_sha256=_checksum(text),
        config=config,
    )


def _cosine(left: DenseEmbedding, right: DenseEmbedding) -> float:
    if left.dimensions != right.dimensions:
        raise EmbeddingSmokeError("Embedding dimensions differ during comparison.")
    score = sum(a * b for a, b in zip(left.vector, right.vector, strict=True))
    if not math.isfinite(score):
        raise EmbeddingSmokeError("Embedding cosine similarity is not finite.")
    return max(-1.0, min(1.0, score))


async def run_smoke(
    provider: DenseEmbeddingProvider,
    *,
    config: EmbeddingConfig,
    fixtures: Sequence[EmbeddingFixture] = FIXTURES,
    minimum_margin: float = DEFAULT_MINIMUM_MARGIN,
) -> dict[str, object]:
    """Run fixed comparisons and return only redacted structural measurements."""

    if not fixtures:
        raise EmbeddingSmokeError("At least one embedding fixture is required.")
    if not math.isfinite(minimum_margin) or minimum_margin < 0:
        raise EmbeddingSmokeError("Embedding minimum margin must be finite and non-negative.")

    cache: dict[str, DenseEmbedding] = {}

    async def embed_once(text: str) -> DenseEmbedding:
        checksum = _checksum(text)
        if checksum not in cache:
            result = await provider.embed(_request(text, config))
            if result.provider_identity != provider.provider_identity:
                raise EmbeddingSmokeError("Embedding provider identity changed during the smoke.")
            if result.model_identity != config.model_identity:
                raise EmbeddingSmokeError("Embedding model identity does not match the config.")
            if result.dimensions != config.dimensions:
                raise EmbeddingSmokeError("Embedding dimensions do not match the config.")
            cache[checksum] = result
        return cache[checksum]

    rows: list[dict[str, object]] = []
    for fixture in fixtures:
        query = await embed_once(fixture.query)
        related = await embed_once(fixture.related)
        distractor = await embed_once(fixture.distractor)
        related_score = _cosine(query, related)
        distractor_score = _cosine(query, distractor)
        margin = related_score - distractor_score
        if margin < minimum_margin:
            raise EmbeddingSmokeError(
                f"Fixture {fixture.label!r} failed its minimum semantic margin."
            )
        rows.append(
            {
                "fixture": fixture.label,
                "query_checksum": _checksum(fixture.query),
                "related_checksum": _checksum(fixture.related),
                "distractor_checksum": _checksum(fixture.distractor),
                "dimensions": query.dimensions,
                "related_cosine": round(related_score, 6),
                "distractor_cosine": round(distractor_score, 6),
                "margin": round(margin, 6),
            }
        )
    return {
        "provider_identity": provider.provider_identity,
        "model_identity": config.model_identity,
        "config_checksum": config.checksum_sha256,
        "fixture_count": len(rows),
        "unique_input_count": len(cache),
        "minimum_margin": minimum_margin,
        "fixtures": rows,
    }


def _parser() -> argparse.ArgumentParser:
    settings = Settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=os.environ.get("OPENWIKIRAG_DENSE_EMBEDDING_MODEL", settings.dense_embedding_model),
    )
    parser.add_argument(
        "--digest",
        default=os.environ.get(
            "OPENWIKIRAG_DENSE_EMBEDDING_MODEL_DIGEST",
            settings.dense_embedding_model_digest,
        ),
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "OPENWIKIRAG_DENSE_EMBEDDING_BASE_URL",
            settings.dense_embedding_base_url,
        ),
    )
    parser.add_argument("--dimensions", type=int, default=settings.dense_embedding_dimensions)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=settings.dense_embedding_timeout_seconds,
    )
    parser.add_argument("--minimum-margin", type=float, default=DEFAULT_MINIMUM_MARGIN)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = EmbeddingConfig(
            model_identity=f"{args.model}@{args.digest}",
            dimensions=args.dimensions,
        )
        provider = OllamaDenseEmbeddingProvider(
            model=args.model,
            digest=args.digest,
            base_url=args.base_url,
            timeout_seconds=args.timeout_seconds,
        )
        report = asyncio.run(
            run_smoke(provider, config=config, minimum_margin=args.minimum_margin)
        )
    except (ValueError, EmbeddingError) as exc:
        print(f"embedding_ollama_smoke_failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
