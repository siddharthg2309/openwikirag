"""Offline contract tests for the redacted live embedding smoke."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from openwikirag.application.embeddings import (
    DenseEmbedding,
    EmbeddingConfig,
    EmbeddingRequest,
)
from ops.embedding_ollama_smoke import EmbeddingFixture, EmbeddingSmokeError, run_smoke


@dataclass
class FixedProvider:
    vectors: Mapping[str, tuple[float, ...]]
    provider_identity: str = "offline-embedding-provider-v1"
    calls: list[str] = field(default_factory=list)

    async def embed(self, request: EmbeddingRequest) -> DenseEmbedding:
        self.calls.append(request.text)
        return DenseEmbedding(
            provider_identity=self.provider_identity,
            model_identity=request.config.model_identity,
            config_checksum_sha256=request.config.checksum_sha256,
            input_checksum_sha256=request.input_checksum_sha256,
            dimensions=request.config.dimensions,
            distance_metric=request.config.distance_metric,
            vector=self.vectors[request.text],
        )


def _fixture() -> EmbeddingFixture:
    return EmbeddingFixture("boundary", "query text", "related text", "distractor text")


def _config() -> EmbeddingConfig:
    return EmbeddingConfig(model_identity="offline-model-v1", dimensions=2)


@pytest.mark.asyncio
async def test_smoke_reports_redacted_scores_and_reuses_exact_inputs() -> None:
    fixture = _fixture()
    provider = FixedProvider(
        {
            fixture.query: (1.0, 0.0),
            fixture.related: (1.0, 0.0),
            fixture.distractor: (0.0, 1.0),
        }
    )

    report = await run_smoke(provider, config=_config(), fixtures=(fixture,))

    encoded = json.dumps(report)
    assert report["fixture_count"] == 1
    assert report["unique_input_count"] == 3
    assert provider.calls == [fixture.query, fixture.related, fixture.distractor]
    assert fixture.query not in encoded
    assert fixture.related not in encoded
    assert fixture.distractor not in encoded
    rows = report["fixtures"]
    assert isinstance(rows, list)
    assert rows[0]["margin"] == 1.0


@pytest.mark.asyncio
async def test_smoke_fails_when_related_text_does_not_beat_distractor() -> None:
    fixture = _fixture()
    provider = FixedProvider(
        {
            fixture.query: (1.0, 0.0),
            fixture.related: (0.0, 1.0),
            fixture.distractor: (1.0, 0.0),
        }
    )

    with pytest.raises(EmbeddingSmokeError, match="minimum semantic margin"):
        await run_smoke(provider, config=_config(), fixtures=(fixture,))
