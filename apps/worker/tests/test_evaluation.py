from pathlib import Path

import pytest

from openwikirag.application.embeddings import (
    DenseEmbedding,
    DenseEmbeddingProvider,
    DeterministicHashEmbeddingProvider,
    EmbeddingConfig,
    EmbeddingRequest,
)
from openwikirag.application.evaluation import EvaluationDataset, evaluate, rank_metrics


def test_rank_metrics_known_positions_and_graded_discount() -> None:
    result = rank_metrics(["noise", "a", "b"], {"a": 3, "b": 1}, k=2)
    assert result["recall"] == 0.5 and result["mrr"] == 0.5
    assert result["ndcg"] == pytest.approx((7 / 1.584962500721156) / (7 + 1 / 1.584962500721156))
    assert rank_metrics(["a", "b"], {"a": 3, "b": 1}, k=2) == {"recall": 1, "mrr": 1, "ndcg": 1}
    assert rank_metrics([], {"a": 1}, k=3) == {"recall": 0, "mrr": 0, "ndcg": 0}


def test_invalid_judgments_and_duplicates_are_rejected() -> None:
    for ranking, grades, k in [
        (["a", "a"], {"a": 1}, 2),
        ([], {"a": 0}, 2),
        ([], {"a": 4}, 2),
        ([], {"a": 1}, 0),
    ]:
        with pytest.raises(ValueError):
            rank_metrics(ranking, grades, k=k)


async def test_authored_fixture_runs_deterministic_baselines() -> None:
    dataset = EvaluationDataset.model_validate_json(Path("evals/retrieval.json").read_bytes())
    first = await evaluate(dataset)
    assert first == await evaluate(dataset)
    assert first["queries"] == 8 and first["documents"] == 10
    assert first["reranker"] is None
    rows = first["per_query"]
    assert isinstance(rows, list)
    exact = [row for row in rows if row["query_id"] == "q6" and row["mode"] == "sparse"][0]
    assert exact["ranking"][0] == "code"


async def test_evaluation_injects_one_dense_provider_for_documents_and_queries() -> None:
    dataset = EvaluationDataset.model_validate_json(Path("evals/retrieval.json").read_bytes())
    baseline = DeterministicHashEmbeddingProvider()

    class RecordingProvider:
        provider_identity = "recording-embedding-provider-v1"

        def __init__(self, wrapped: DenseEmbeddingProvider) -> None:
            self.wrapped = wrapped
            self.requests: list[str] = []

        async def embed(self, request: EmbeddingRequest) -> DenseEmbedding:
            self.requests.append(request.text)
            return await self.wrapped.embed(request)

    provider = RecordingProvider(baseline)
    result = await evaluate(
        dataset,
        dense_provider=provider,
        dense_config=EmbeddingConfig(model_identity="recorded-hash-v1", dimensions=128),
    )

    assert result["dense_model"] == "recorded-hash-v1"
    assert result["dense_provider"] == provider.provider_identity
    assert len(provider.requests) > len(dataset.documents)
    assert all(isinstance(text, str) and text for text in provider.requests)


async def test_evaluation_preserves_deterministic_output_when_provider_is_explicit() -> None:
    dataset = EvaluationDataset.model_validate_json(Path("evals/retrieval.json").read_bytes())
    implicit = await evaluate(dataset)
    explicit = await evaluate(
        dataset,
        dense_provider=DeterministicHashEmbeddingProvider(),
        dense_config=EmbeddingConfig(),
    )

    assert explicit == implicit
