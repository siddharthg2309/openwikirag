"""Proof for the optional Ollama structured WikiRAG provider."""

import json

import httpx
import pytest

from apps.worker.app.main import build_wiki_generation_provider
from openwikirag.application.extraction import MarkdownExtractor
from openwikirag.application.metadata import DeterministicMetadataExtractor
from openwikirag.application.wiki import WikiPageBuilder
from openwikirag.application.wiki_generation import (
    DeterministicWikiProvider,
    GeneratedWikiContent,
    InvalidWikiGenerationOutputError,
    StructuredWikiGenerator,
    WikiGenerationProviderError,
    WikiGenerationRequest,
)
from openwikirag.core.config import Settings
from openwikirag.infrastructure.wiki_ollama import OllamaWikiProvider

MODEL = "qwen2.5:7b"
DIGEST = "a" * 64
CONFIG_HASH = "c" * 64


def _request() -> WikiGenerationRequest:
    document = MarkdownExtractor().extract(b"# Overview\nOpenWikiRAG uses citations.\n")
    metadata = DeterministicMetadataExtractor().extract(document=document)
    page = WikiPageBuilder().build(document=document, metadata=metadata)
    return WikiGenerationRequest(
        page=page,
        document_text=document.text,
        config_hash=CONFIG_HASH,
    )


def _transport(
    *,
    drift_after_chat: bool = False,
    chat_status: int = 200,
    chat_content: str | None = None,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    calls: list[httpx.Request] = []
    tag_calls = 0
    generated_content = chat_content or json.dumps(
        {"summary": None, "definitions": [], "references": []}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tag_calls
        calls.append(request)
        if request.url.path == "/api/tags":
            tag_calls += 1
            digest = "b" * 64 if drift_after_chat and tag_calls == 2 else DIGEST
            return httpx.Response(
                200,
                json={"models": [{"name": MODEL, "digest": digest}]},
                request=request,
            )
        if request.url.path == "/api/chat":
            return httpx.Response(
                chat_status,
                json={
                    "model": MODEL,
                    "done": True,
                    "message": {"content": generated_content},
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    return httpx.MockTransport(handler), calls


def _provider(transport: httpx.BaseTransport) -> OllamaWikiProvider:
    return OllamaWikiProvider(
        model=MODEL,
        digest=DIGEST,
        base_url="http://ollama.test",
        transport=transport,
    )


def test_provider_runs_digest_checked_structured_exchange() -> None:
    transport, calls = _transport()
    provider = _provider(transport)
    request = _request()

    result = StructuredWikiGenerator(provider=provider).generate(
        document=MarkdownExtractor().extract(request.document_text.encode()),
        page=request.page,
        config_hash=CONFIG_HASH,
    )

    assert result.provider_identity == provider.provider_identity
    assert [call.url.path for call in calls] == ["/api/tags", "/api/chat", "/api/tags"]
    chat_payload = json.loads(calls[1].content)
    assert chat_payload["model"] == MODEL
    assert chat_payload["format"] == GeneratedWikiContent.model_json_schema()
    assert chat_payload["stream"] is False
    assert chat_payload["options"] == {
        "temperature": 0,
        "num_ctx": 8192,
        "num_predict": 1024,
    }
    assert "document_data" in chat_payload["messages"][1]["content"]
    assert "OpenWikiRAG uses citations." in chat_payload["messages"][1]["content"]
    assert "untrusted content" in chat_payload["messages"][0]["content"]
    assert "section_path" in chat_payload["messages"][1]["content"]
    assert "return null/empty generated fields" in chat_payload["messages"][1]["content"]
    assert "source_span_catalog" in chat_payload["messages"][1]["content"]
    assert "wiki-generation-prompt-v3" in chat_payload["messages"][1]["content"]


def test_provider_rejects_model_digest_drift_after_inference() -> None:
    transport, calls = _transport(drift_after_chat=True)

    with pytest.raises(InvalidWikiGenerationOutputError, match="digest changed"):
        _provider(transport).generate(request=_request())

    assert [call.url.path for call in calls] == ["/api/tags", "/api/chat", "/api/tags"]


@pytest.mark.parametrize("status", [429, 503])
def test_provider_maps_transient_http_failures_to_retryable_error(status: int) -> None:
    transport, _ = _transport(chat_status=status)

    with pytest.raises(WikiGenerationProviderError, match="temporarily unavailable"):
        _provider(transport).generate(request=_request())


def test_provider_maps_transport_failure_to_retryable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(WikiGenerationProviderError, match="unavailable"):
        _provider(httpx.MockTransport(handler)).generate(request=_request())


def test_provider_rejects_malformed_or_oversized_model_output() -> None:
    transport, _ = _transport(chat_content="not-json")
    request = _request()
    document = MarkdownExtractor().extract(request.document_text.encode())
    with pytest.raises(InvalidWikiGenerationOutputError, match="valid JSON"):
        StructuredWikiGenerator(provider=_provider(transport)).generate(
            document=document,
            page=request.page,
            config_hash=CONFIG_HASH,
        )

    oversized_transport, _ = _transport(chat_content="x" * 132_000)
    with pytest.raises(InvalidWikiGenerationOutputError, match="byte limit"):
        _provider(oversized_transport).generate(request=_request())


def test_provider_constructor_and_settings_require_safe_complete_identity() -> None:
    with pytest.raises(ValueError, match=r"HTTP\(S\)"):
        OllamaWikiProvider(model=MODEL, digest=DIGEST, base_url="file:///tmp/model")
    with pytest.raises(ValueError, match="digest"):
        OllamaWikiProvider(model=MODEL, digest="unsafe")
    with pytest.raises(ValueError, match="together"):
        Settings(wiki_generation_model=MODEL)


def test_worker_composition_keeps_deterministic_default_and_selects_ollama() -> None:
    default = build_wiki_generation_provider(Settings())
    configured = build_wiki_generation_provider(
        Settings(
            wiki_generation_model=MODEL,
            wiki_generation_model_digest=DIGEST,
            wiki_generation_base_url="http://ollama.test",
        )
    )

    assert isinstance(default, DeterministicWikiProvider)
    assert isinstance(configured, OllamaWikiProvider)
