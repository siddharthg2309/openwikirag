"""Run a redacted, evidence-bearing smoke against a pinned Ollama WikiRAG model."""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

from openwikirag.application.extraction import MarkdownExtractor
from openwikirag.application.metadata import DeterministicMetadataExtractor
from openwikirag.application.wiki import WikiPageBuilder
from openwikirag.application.wiki_generation import (
    StructuredWikiGenerator,
    WikiGenerationError,
    WikiGenerationProvider,
)
from openwikirag.core.config import Settings
from openwikirag.infrastructure.wiki_ollama import OllamaWikiProvider

FIXTURES: tuple[tuple[str, bytes], ...] = (
    (
        "storage",
        b"# Storage\nRaw uploads remain outside PostgreSQL. PostgreSQL stores "
        b"immutable metadata and checksums.\n",
    ),
    (
        "ingestion",
        b"# Ingestion\nRedis Streams delivers at-least-once jobs to a durable "
        b"worker. Duplicate events are safe to replay.\n",
    ),
    (
        "retrieval",
        b"# Retrieval\nDense and lexical retrieval are fused before citation "
        b"validation. Answers use canonical tenant-scoped evidence.\n",
    ),
)


def _evidence_count(content: Any) -> int:
    count = 0
    if content.summary is not None:
        count += len(content.summary.evidence)
    count += sum(len(item.evidence) for item in content.definitions)
    count += sum(len(item.evidence) for item in content.references)
    return count


def run_smoke(
    provider: WikiGenerationProvider,
    *,
    config_hash: str,
    fixtures: Sequence[tuple[str, bytes]] = FIXTURES,
) -> dict[str, object]:
    """Run fixed fixtures and return only redacted structural evidence."""

    generator = StructuredWikiGenerator(provider=provider)
    results: list[dict[str, object]] = []
    for fixture_name, source in fixtures:
        document = MarkdownExtractor().extract(source)
        page = WikiPageBuilder().build(
            document=document,
            metadata=DeterministicMetadataExtractor().extract(document=document),
        )
        result = generator.generate(
            document=document,
            page=page,
            config_hash=config_hash,
        )
        evidence_count = _evidence_count(result.content)
        if evidence_count < 1:
            raise WikiGenerationError(f"Fixture {fixture_name!r} produced no evidence.")
        results.append(
            {
                "fixture": fixture_name,
                "source_checksum": result.source_artifact_checksum,
                "result_checksum": result.checksum_sha256,
                "prompt_checksum": result.prompt_checksum,
                "evidence_count": evidence_count,
                "summary_present": result.content.summary is not None,
                "definition_count": len(result.content.definitions),
                "reference_count": len(result.content.references),
            }
        )
    return {
        "provider_identity": provider.provider_identity,
        "fixture_count": len(results),
        "fixtures": results,
    }


def _parser() -> argparse.ArgumentParser:
    settings = Settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=os.environ.get(
            "OPENWIKIRAG_WIKI_GENERATION_MODEL",
            settings.wiki_generation_model,
        ),
    )
    parser.add_argument(
        "--digest",
        default=os.environ.get(
            "OPENWIKIRAG_WIKI_GENERATION_MODEL_DIGEST",
            settings.wiki_generation_model_digest,
        ),
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "OPENWIKIRAG_WIKI_GENERATION_BASE_URL",
            settings.wiki_generation_base_url,
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=settings.wiki_generation_timeout_seconds,
    )
    parser.add_argument("--config-hash", default=settings.wiki_generation_config_hash)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        provider = OllamaWikiProvider(
            model=args.model,
            digest=args.digest,
            base_url=args.base_url,
            timeout_seconds=args.timeout_seconds,
        )
        report = run_smoke(provider, config_hash=args.config_hash)
    except (ValueError, WikiGenerationError) as exc:
        print(f"wiki_ollama_smoke_failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
