"""Exact support, budget, provider failure and opt-in real local LLM proof."""

import asyncio
import json
import os
from uuid import uuid4

import httpx
import pytest

from apps.worker.tests.test_vector_index import _request
from openwikirag.application.answers import (
    AnswerContext,
    DraftAnswer,
    DraftClaim,
    GenerationInputError,
    GenerationOutputError,
    GenerationRetryableError,
    GroundedGenerationService,
    pack_context,
    validate_draft,
)
from openwikirag.application.evidence import ResolvedEvidence
from openwikirag.application.source_evidence import SourceEvidence
from openwikirag.infrastructure.ollama import OllamaAnswerProvider


async def answer_context(query: str = "What is the capital of France?") -> AnswerContext:
    req = await _request(text="Paris is the capital of France. Berlin is the capital of Germany.")
    source = SourceEvidence.from_retrieval(
        ResolvedEvidence(
            manifest_id=uuid4(),
            payload=req.build_point().payload,
            chunk=req.chunk,
        )
    )
    return pack_context(tenant_id=source.tenant_id, query=query, evidence=(source,))


class QuoteProvider:
    identity = "fixture-quote-provider-v1"

    def __init__(self, failures: int = 0):
        self.calls, self.failures = 0, failures

    async def generate(self, context: AnswerContext) -> DraftAnswer:
        self.calls += 1
        if self.calls <= self.failures:
            raise GenerationRetryableError("outage")
        return DraftAnswer(
            status="answered",
            claims=(
                DraftClaim(
                    evidence_id="E1",
                    quote="Paris is the capital of France.",
                ),
            ),
        )


async def test_exact_citation_quote_rendering_and_offsets() -> None:
    context = await answer_context()
    result = await GroundedGenerationService(QuoteProvider()).generate(context)
    assert result.text == "Paris is the capital of France. [1]"
    assert result.citations[0].start_char == context.passages[0].source.chunk.normalized_start_char
    assert result.citations[0].end_char - result.citations[0].start_char == len(
        result.citations[0].quote
    )
    assert result.prompt_bytes <= 6000 and result.attempts == 1


@pytest.mark.parametrize(
    "identity, quote", [("E8", "Paris"), (None, "London is France's capital."), (None, " ")]
)
async def test_invented_reference_quote_and_blank_claim_rejected(
    identity: str | None, quote: str
) -> None:
    context = await answer_context()
    draft = DraftAnswer(
        status="answered",
        claims=(
            DraftClaim(
                evidence_id=identity or "E1",
                quote=quote,
            ),
        ),
    )
    with pytest.raises(GenerationOutputError):
        validate_draft(context, draft, provider="test", attempts=1)


async def test_context_tenant_unicode_budget_and_no_evidence_short_circuit() -> None:
    context = await answer_context()
    source = context.passages[0].source
    with pytest.raises(GenerationInputError):
        pack_context(tenant_id=uuid4(), query="question", evidence=(source,))
    with pytest.raises(GenerationInputError):
        pack_context(tenant_id=source.tenant_id, query="漢" * 4000, evidence=())
    repeated = pack_context(tenant_id=source.tenant_id, query="capital", evidence=(source, source))
    assert len(repeated.passages) == 1
    provider = QuoteProvider()
    result = await GroundedGenerationService(provider).generate(
        pack_context(
            tenant_id=source.tenant_id,
            query="question",
            evidence=(),
        )
    )
    assert result.status == "insufficient_evidence" and result.attempts == 0 and provider.calls == 0


async def test_transient_retry_budget_and_timeout() -> None:
    context = await answer_context()
    provider = QuoteProvider(failures=1)
    assert (await GroundedGenerationService(provider).generate(context)).attempts == 2
    assert provider.calls == 2
    provider = QuoteProvider(failures=3)
    with pytest.raises(GenerationRetryableError):
        await GroundedGenerationService(provider).generate(context)
    assert provider.calls == 2

    class Slow(QuoteProvider):
        async def generate(self, context: AnswerContext) -> DraftAnswer:
            await asyncio.sleep(1)
            return await super().generate(context)

    with pytest.raises(GenerationRetryableError):
        await GroundedGenerationService(Slow(), timeout_seconds=0.001, max_attempts=1).generate(
            context
        )


async def test_ollama_digest_schema_and_response_bounds() -> None:
    context = await answer_context()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "test", "digest": "a" * 64}]})
        body = json.loads(request.content)
        assert body["stream"] is False and body["format"]["type"] == "object"
        return httpx.Response(
            200,
            json={
                "model": "test",
                "done": True,
                "message": {
                    "content": DraftAnswer(
                        status="insufficient_evidence", claims=()
                    ).model_dump_json(),
                },
            },
        )

    provider = OllamaAnswerProvider(
        model="test", digest="a" * 64, transport=httpx.MockTransport(handler)
    )
    assert (await provider.generate(context)).status == "insufficient_evidence"
    assert len(requests) == 3
    provider = OllamaAnswerProvider(
        model="test", digest="b" * 64, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(GenerationOutputError, match="digest"):
        await provider.generate(context)

    async def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 65537)

    provider = OllamaAnswerProvider(
        model="test", digest="a" * 64, transport=httpx.MockTransport(oversized)
    )
    with pytest.raises(GenerationOutputError, match="byte limit"):
        await provider.generate(context)


async def test_ollama_protocol_retry_and_post_inference_digest_drift() -> None:
    posts = 0
    drift = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.url.path == "/api/tags":
            digest = "b" * 64 if drift and posts >= 3 else "a" * 64
            return httpx.Response(200, json={"models": [{"name": "test", "digest": digest}]})
        posts += 1
        if posts == 1:
            raise httpx.RemoteProtocolError("connection closed during response")
        return httpx.Response(
            200,
            json={
                "model": "test",
                "done": True,
                "message": {"content": '{"status":"insufficient_evidence","claims":[]}'},
            },
        )

    provider = OllamaAnswerProvider(
        model="test", digest="a" * 64, transport=httpx.MockTransport(handler)
    )
    service = GroundedGenerationService(provider)
    assert (await service.generate(await answer_context())).attempts == 2
    assert posts == 2
    drift = True
    with pytest.raises(GenerationOutputError, match="digest changed"):
        await service.generate(await answer_context())


@pytest.mark.skipif(not os.getenv("OPENWIKIRAG_TEST_OLLAMA_MODEL"), reason="real local LLM opt-in")
async def test_real_ollama_supported_and_insufficient_evidence() -> None:
    provider = OllamaAnswerProvider(
        model=os.environ["OPENWIKIRAG_TEST_OLLAMA_MODEL"],
        digest=os.environ["OPENWIKIRAG_TEST_OLLAMA_DIGEST"],
    )
    service = GroundedGenerationService(provider, timeout_seconds=120, max_attempts=1)
    answer = await service.generate(await answer_context())
    assert answer.status == "answered" and "Paris" in answer.text
    unsupported = await service.generate(
        await answer_context("What is the approved 2035 annual budget of OpenWikiRAG?")
    )
    assert unsupported.status == "insufficient_evidence" and not unsupported.citations
