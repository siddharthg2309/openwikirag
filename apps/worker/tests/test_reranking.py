"""Pairwise reranking proof; real model execution is opt-in and separately labeled."""

import asyncio
import os
import threading
from uuid import uuid4

import pytest

from apps.worker.tests.test_vector_index import FOREIGN_TENANT_ID, TENANT_ID, _request
from openwikirag.application.evidence import ResolvedEvidence
from openwikirag.application.reranking import (
    RerankingConfig,
    RerankingInputError,
    RerankingProviderError,
    RerankingService,
)
from openwikirag.infrastructure.reranking import SentenceTransformersReranker


async def _evidence() -> tuple[ResolvedEvidence, ...]:
    requests = (
        await _request(text="Authentication tokens rotate."),
        await _request(text="Bananas are a yellow fruit."),
    )
    return tuple(
        ResolvedEvidence(manifest_id=uuid4(), payload=req.build_point().payload, chunk=req.chunk)
        for req in requests
    )


class FakeReranker:
    identity = "fixture-scorer-v1"

    def __init__(self, scores: tuple[float, ...] = (0.1, 0.9)) -> None:
        self.scores = scores
        self.pairs: tuple[tuple[str, str], ...] = ()

    async def score(self, pairs: tuple[tuple[str, str], ...]) -> tuple[float, ...]:
        self.pairs = pairs
        return self.scores


async def test_reranking_reorders_pairs_and_preserves_canonical_evidence() -> None:
    evidence = await _evidence()
    provider = FakeReranker()
    results = await RerankingService(provider).rerank(
        tenant_id=TENANT_ID,
        query="  yellow  fruit ",
        evidence=evidence,
        limit=2,
    )
    assert results[0].evidence == evidence[1]
    assert [item.rank for item in results] == [1, 2]
    assert [item.source_rank for item in results] == [2, 1]
    assert provider.pairs == tuple(("yellow fruit", item.chunk.text) for item in evidence)


async def test_equal_scores_preserve_fused_order_and_empty_calls_no_provider() -> None:
    evidence = await _evidence()
    provider = FakeReranker((0.5, 0.5))
    service = RerankingService(provider)
    assert await service.rerank(tenant_id=TENANT_ID, query="tokens", evidence=()) == ()
    assert provider.pairs == ()
    results = await service.rerank(tenant_id=TENANT_ID, query="tokens", evidence=evidence, limit=1)
    assert results[0].evidence == evidence[0]


@pytest.mark.parametrize("scores", [(0.1,), (float("nan"), 1.0), (float("inf"), 1.0)])
async def test_malformed_provider_scores_fail_closed(scores: tuple[float, ...]) -> None:
    with pytest.raises(RerankingProviderError):
        await RerankingService(FakeReranker(scores)).rerank(
            tenant_id=TENANT_ID,
            query="tokens",
            evidence=await _evidence(),
        )


async def test_foreign_duplicate_oversized_and_bad_queries_fail_before_provider() -> None:
    evidence = await _evidence()
    provider = FakeReranker()
    service = RerankingService(provider)
    for tenant, query, candidates in (
        (FOREIGN_TENANT_ID, "tokens", evidence),
        (TENANT_ID, "tokens", (evidence[0], evidence[0])),
        (TENANT_ID, "\x00", evidence),
        (TENANT_ID, " ", evidence),
        (TENANT_ID, "tokens", evidence * 21),
    ):
        with pytest.raises(RerankingInputError):
            await service.rerank(tenant_id=tenant, query=query, evidence=candidates)
    with pytest.raises(RerankingInputError):
        await RerankingService(provider, RerankingConfig(max_passage_characters=1)).rerank(
            tenant_id=TENANT_ID,
            query="tokens",
            evidence=evidence,
        )
    assert provider.pairs == ()


async def test_provider_exception_and_timeout_are_explicit() -> None:
    class FailingProvider(FakeReranker):
        async def score(self, pairs: tuple[tuple[str, str], ...]) -> tuple[float, ...]:
            raise RuntimeError("private error details")

    class SlowProvider(FakeReranker):
        async def score(self, pairs: tuple[tuple[str, str], ...]) -> tuple[float, ...]:
            await asyncio.Event().wait()
            return ()

    for provider in (FailingProvider(), SlowProvider()):
        with pytest.raises(RerankingProviderError, match="provider failed"):
            await RerankingService(provider, RerankingConfig(timeout_seconds=0.01)).rerank(
                tenant_id=TENANT_ID,
                query="tokens",
                evidence=await _evidence(),
            )


async def test_cancellation_does_not_free_busy_cpu_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = SentenceTransformersReranker(model="test", revision="a" * 40)
    release = threading.Event()
    started = asyncio.Event()
    loop = asyncio.get_running_loop()

    def predict(pairs: tuple[tuple[str, str], ...]) -> tuple[float, ...]:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5)
        return (1.0,)

    monkeypatch.setattr(provider, "_predict", predict)
    task = asyncio.create_task(provider.score((("query", "passage"),)))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(RerankingProviderError, match="busy"):
            await provider.score((("query", "passage"),))
    finally:
        release.set()
        assert provider._active is not None
        await provider._active


def test_configuration_bounds_and_immutable_revision() -> None:
    for config in (
        {"max_candidates": 0},
        {"max_candidates": True},
        {"timeout_seconds": float("nan")},
        {"max_passage_characters": 0},
    ):
        with pytest.raises(RerankingInputError):
            RerankingConfig(**config)
    with pytest.raises(ValueError, match="revision"):
        SentenceTransformersReranker(model="test", revision="main")


@pytest.mark.skipif(not os.getenv("OPENWIKIRAG_TEST_RERANKER_MODEL"), reason="real model opt-in")
async def test_real_cross_encoder_scores_relevant_evidence_above_distractor() -> None:
    provider = SentenceTransformersReranker(
        model=os.environ["OPENWIKIRAG_TEST_RERANKER_MODEL"],
        revision=os.environ["OPENWIKIRAG_TEST_RERANKER_REVISION"],
        allow_download=True,
    )
    scores = await provider.score(
        (
            ("What is the capital of France?", "Paris is the capital of France."),
            ("What is the capital of France?", "Bananas grow in tropical climates."),
        )
    )
    assert len(scores) == 2 and scores[0] > scores[1]
