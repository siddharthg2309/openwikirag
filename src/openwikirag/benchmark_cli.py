"""Run a bounded, reproducible local retrieval benchmark.

Example:
    python -m openwikirag.benchmark_cli --documents 25 --queries 4 \
        --warmup 3 --iterations 20
"""

import argparse
import asyncio
import json
import math
import statistics
import time
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from openwikirag.application.chunking import HierarchicalChunker
from openwikirag.application.deduplication import deduplicate_evidence
from openwikirag.application.embeddings import (
    DeterministicHashEmbeddingProvider,
    EmbeddingConfig,
    EmbeddingRequest,
)
from openwikirag.application.extraction import MarkdownExtractor
from openwikirag.application.fusion import ReciprocalRankFusionService
from openwikirag.application.metadata import DeterministicMetadataExtractor
from openwikirag.application.retrieval import (
    CandidateRetrievalService,
    InMemoryCandidateIndex,
    RetrievalMode,
    SearchRequest,
)
from openwikirag.application.sparse import (
    DeterministicHashSparseEmbeddingProvider,
    SparseEmbeddingConfig,
    SparseEmbeddingRequest,
)
from openwikirag.application.vector_index import (
    InMemoryVectorIndex,
    VectorCollectionConfig,
    VectorPoint,
    VectorPointRequest,
)

BENCHMARK_SCHEMA_VERSION = "benchmark-v1"
BENCHMARK_TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
MAX_DOCUMENTS = 500
MAX_QUERIES = 20
MAX_WARMUP_ITERATIONS = 100
MAX_MEASURED_ITERATIONS = 1_000
MAX_CONCURRENCY = 32
BENCHMARK_MODES: tuple[RetrievalMode, ...] = ("dense", "sparse", "hybrid")


class BenchmarkInputError(ValueError):
    """Raised when a benchmark workload or result cannot be trusted."""


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    """Bounded workload parameters for one local benchmark run."""

    documents: int = 25
    queries: int = 4
    warmup: int = 3
    iterations: int = 20
    concurrency: int = 1

    def __post_init__(self) -> None:
        bounds = (
            ("documents", self.documents, 1, MAX_DOCUMENTS),
            ("queries", self.queries, 1, MAX_QUERIES),
            ("warmup", self.warmup, 0, MAX_WARMUP_ITERATIONS),
            ("iterations", self.iterations, 1, MAX_MEASURED_ITERATIONS),
            ("concurrency", self.concurrency, 1, MAX_CONCURRENCY),
        )
        for name, value, minimum, maximum in bounds:
            if type(value) is not int or not minimum <= value <= maximum:
                raise BenchmarkInputError(
                    f"{name} must be an integer between {minimum} and {maximum}."
                )


def _percentile(samples: tuple[float, ...], quantile: float) -> float:
    if not samples or not 0.0 <= quantile <= 1.0:
        raise BenchmarkInputError("Percentiles require non-empty samples and a [0, 1] quantile.")
    ordered = sorted(samples)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _summarize(
    samples: tuple[float, ...], *, operations_per_sample: int
) -> dict[str, float | int | str]:
    if not samples or any(not math.isfinite(value) or value < 0.0 for value in samples):
        raise BenchmarkInputError("Benchmark timings must be finite and non-negative.")
    if type(operations_per_sample) is not int or operations_per_sample < 1:
        raise BenchmarkInputError("Benchmark operations per sample must be positive.")
    mean_seconds = statistics.fmean(samples)
    return {
        "unit": "milliseconds",
        "sample_count": len(samples),
        "operations_per_sample": operations_per_sample,
        "min": min(samples) * 1_000,
        "mean": mean_seconds * 1_000,
        "p50": _percentile(samples, 0.50) * 1_000,
        "p95": _percentile(samples, 0.95) * 1_000,
        "max": max(samples) * 1_000,
        "operations_per_second": operations_per_sample / mean_seconds,
    }


async def _build_points(document_count: int) -> tuple[VectorPoint, ...]:
    if type(document_count) is not int or not 1 <= document_count <= MAX_DOCUMENTS:
        raise BenchmarkInputError("The document workload is outside the supported bound.")

    extractor = MarkdownExtractor()
    metadata_extractor = DeterministicMetadataExtractor()
    dense_provider = DeterministicHashEmbeddingProvider()
    sparse_provider = DeterministicHashSparseEmbeddingProvider()
    dense_config = EmbeddingConfig()
    sparse_config = SparseEmbeddingConfig(index_space_size=2**20)
    collection = VectorCollectionConfig()
    points: list[VectorPoint] = []

    for index in range(document_count):
        document_id = uuid5(NAMESPACE_URL, f"openwikirag-benchmark-document:{index}")
        version_id = uuid5(NAMESPACE_URL, f"openwikirag-benchmark-version:{index}")
        document = extractor.extract(
            (
                f"# Topic {index}\n"
                f"This document records immutable provenance for deterministic retrieval "
                f"benchmark topic {index}.\n"
            ).encode()
        )
        metadata = metadata_extractor.extract(document=document)
        chunks = HierarchicalChunker().chunk(document)
        for chunk in chunks.chunks:
            dense = await dense_provider.embed(
                EmbeddingRequest(
                    text=chunk.text,
                    input_checksum_sha256=chunk.content_checksum_sha256,
                    config=dense_config,
                )
            )
            sparse = await sparse_provider.embed(
                SparseEmbeddingRequest(
                    text=chunk.text,
                    input_checksum_sha256=chunk.content_checksum_sha256,
                    config=sparse_config,
                )
            )
            points.append(
                VectorPointRequest(
                    tenant_id=BENCHMARK_TENANT_ID,
                    document_id=document_id,
                    document_version_id=version_id,
                    chunk=chunk,
                    metadata=metadata,
                    pipeline_version=BENCHMARK_SCHEMA_VERSION,
                    collection=collection,
                    dense=dense,
                    sparse=sparse,
                ).build_point()
            )
    return tuple(points)


def _queries(document_count: int, query_count: int) -> tuple[str, ...]:
    return tuple(
        "immutable provenance deterministic retrieval benchmark topic "
        f"{index % document_count}"
        for index in range(query_count)
    )


async def _measure_mode(
    service: CandidateRetrievalService,
    *,
    mode: RetrievalMode,
    queries: tuple[str, ...],
    warmup: int,
    iterations: int,
    concurrency: int,
) -> dict[str, dict[str, float | int | str]]:
    query_requests = tuple(
        SearchRequest(
            tenant_id=BENCHMARK_TENANT_ID,
            query=query,
            mode=mode,
            candidate_limit=20,
        )
        for query in queries
    )
    requests = tuple(query_requests[index % len(query_requests)] for index in range(concurrency))
    fusion = ReciprocalRankFusionService()

    for _ in range(warmup):
        retrievals = await asyncio.gather(*(service.retrieve(request) for request in requests))
        for retrieval in retrievals:
            deduplicate_evidence(fusion.fuse(retrieval))

    retrieval_samples: list[float] = []
    fusion_samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        results = await asyncio.gather(*(service.retrieve(request) for request in requests))
        retrieval_samples.append(time.perf_counter() - started)

        started = time.perf_counter()
        for result in results:
            deduplicate_evidence(fusion.fuse(result))
        fusion_samples.append(time.perf_counter() - started)

    return {
        "candidate_retrieval": _summarize(
            tuple(retrieval_samples), operations_per_sample=concurrency
        ),
        "fusion_deduplication": _summarize(
            tuple(fusion_samples), operations_per_sample=concurrency
        ),
    }


async def run_benchmark(config: BenchmarkConfig) -> dict[str, object]:
    """Run the bounded benchmark and return a JSON-compatible report."""

    if not isinstance(config, BenchmarkConfig):
        raise BenchmarkInputError("Benchmark configuration is invalid.")
    points = await _build_points(config.documents)
    if not points:
        raise BenchmarkInputError("The benchmark corpus is empty.")
    index = InMemoryVectorIndex()
    for point in points:
        await index.upsert(point)
    service = CandidateRetrievalService(InMemoryCandidateIndex(index.points))
    queries = _queries(config.documents, config.queries)
    stages: dict[str, object] = {}
    for mode in BENCHMARK_MODES:
        stages[mode] = await _measure_mode(
            service,
            mode=mode,
            queries=queries,
            warmup=config.warmup,
            iterations=config.iterations,
            concurrency=config.concurrency,
        )
    return {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "workload": {
            "documents": config.documents,
            "vector_points": len(points),
            "queries_per_sample": config.queries,
            "concurrency": config.concurrency,
            "requests_per_sample": config.concurrency,
            "warmup_iterations": config.warmup,
            "measured_iterations": config.iterations,
            "tenant_scope": "one synthetic tenant",
        },
        "stages": stages,
        "caveats": [
            "Local deterministic providers and in-memory index only.",
            "Excludes network, database, external vector store, model, and process overhead.",
            "Development regression evidence, not a production SLO or capacity guarantee.",
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=int, default=25)
    parser.add_argument("--queries", type=int, default=4)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        config = BenchmarkConfig(
            documents=args.documents,
            queries=args.queries,
            warmup=args.warmup,
            iterations=args.iterations,
            concurrency=args.concurrency,
        )
        report = asyncio.run(run_benchmark(config))
    except BenchmarkInputError as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
