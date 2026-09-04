"""Small reproducible retrieval evaluations; not a production-quality claim."""

import hashlib
import math
from collections.abc import Mapping, Sequence
from typing import Self
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openwikirag.application.chunking import HierarchicalChunker
from openwikirag.application.embeddings import DeterministicHashEmbeddingProvider, EmbeddingRequest
from openwikirag.application.extraction import PlainTextExtractor
from openwikirag.application.fusion import ReciprocalRankFusionService, RrfFusionConfig
from openwikirag.application.metadata import DeterministicMetadataExtractor
from openwikirag.application.reranking import PairwiseReranker
from openwikirag.application.retrieval import (
    CandidateRetrievalConfig,
    CandidateRetrievalService,
    InMemoryCandidateIndex,
    SearchRequest,
)
from openwikirag.application.sparse import (
    DeterministicHashSparseEmbeddingProvider,
    SparseEmbeddingRequest,
)
from openwikirag.application.vector_index import VectorPointRequest


class JudgedQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=4096)
    grades: dict[str, int]


class EvaluationDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    documents: dict[str, str]
    queries: tuple[JudgedQuery, ...]

    @model_validator(mode="after")
    def validate_labels(self) -> Self:
        if not 1 <= len(self.documents) <= 100 or not 1 <= len(self.queries) <= 100:
            raise ValueError("Evaluation supports 1–100 documents and queries.")
        if len({query.id for query in self.queries}) != len(self.queries):
            raise ValueError("Duplicate query ids.")
        if any(
            not key or not text.strip() or len(text) > 8000 for key, text in self.documents.items()
        ):
            raise ValueError("Invalid document text.")
        for query in self.queries:
            rank_metrics((), query.grades, k=1)
            if not set(query.grades) <= self.documents.keys():
                raise ValueError("Judgments reference missing documents.")
        return self


def rank_metrics(ranking: Sequence[str], grades: Mapping[str, int], *, k: int) -> dict[str, float]:
    if type(k) is not int or not 1 <= k <= 100 or len(set(ranking)) != len(ranking):
        raise ValueError("Invalid cutoff or repeated ranking id.")
    if (
        not grades
        or any(type(grade) is not int or not 0 <= grade <= 3 for grade in grades.values())
        or not any(grades.values())
    ):
        raise ValueError("Expected graded judgments with at least one relevant document.")
    top = ranking[:k]
    relevant = {key for key, grade in grades.items() if grade > 0}
    hits = [i for i, key in enumerate(top, 1) if key in relevant]
    dcg = sum((2 ** grades.get(key, 0) - 1) / math.log2(i + 1) for i, key in enumerate(top, 1))
    ideal = sum(
        (2**grade - 1) / math.log2(i + 1)
        for i, grade in enumerate(sorted(grades.values(), reverse=True)[:k], 1)
    )
    return {
        "recall": len(hits) / len(relevant),
        "mrr": 1 / hits[0] if hits else 0.0,
        "ndcg": dcg / ideal if ideal else 0.0,
    }


async def evaluate(
    dataset: EvaluationDataset,
    *,
    k: int = 3,
    reranker: PairwiseReranker | None = None,
) -> dict[str, object]:
    dataset = EvaluationDataset.model_validate(dataset.model_dump())
    rank_metrics((), dataset.queries[0].grades, k=k)
    config = CandidateRetrievalConfig()
    tenant_id = uuid5(NAMESPACE_URL, "openwikirag:eval")
    points, labels, texts = [], {}, {}
    for label, text in sorted(dataset.documents.items()):
        document = PlainTextExtractor().extract(text.encode())
        chunk = HierarchicalChunker().chunk(document).chunks[0]
        if chunk.text != document.text:
            raise ValueError("Evaluation fixture must fit a single canonical parent chunk.")
        dense = await DeterministicHashEmbeddingProvider().embed(
            EmbeddingRequest(
                text=chunk.text,
                input_checksum_sha256=chunk.content_checksum_sha256,
                config=config.dense,
            )
        )
        sparse = await DeterministicHashSparseEmbeddingProvider().embed(
            SparseEmbeddingRequest(
                text=chunk.text,
                input_checksum_sha256=chunk.content_checksum_sha256,
                config=config.sparse,
            )
        )
        point = VectorPointRequest(
            tenant_id=tenant_id,
            document_id=uuid5(tenant_id, label),
            document_version_id=uuid5(tenant_id, label + ":1"),
            chunk=chunk,
            metadata=DeterministicMetadataExtractor().extract(document=document),
            pipeline_version="ingestion-v1",
            collection=config.collection,
            dense=dense,
            sparse=sparse,
        ).build_point()
        points.append(point)
        labels[point.point_id], texts[point.point_id] = label, chunk.text
    service = CandidateRetrievalService(InMemoryCandidateIndex(points), config=config)
    rows: list[dict[str, object]] = []
    totals: dict[str, list[dict[str, float]]] = {mode: [] for mode in ("dense", "sparse", "hybrid")}
    if reranker:
        totals["reranked"] = []
    for query in dataset.queries:
        for mode in ("dense", "sparse", "hybrid"):
            request = SearchRequest(
                tenant_id=tenant_id, query=query.text, mode=mode, candidate_limit=len(points)
            )
            result = await service.retrieve(request)
            fused = ReciprocalRankFusionService(RrfFusionConfig(fused_limit=len(points))).fuse(
                result
            )
            ids = [candidate.point_id for candidate in fused.candidates]
            ranking = [labels[item] for item in ids]
            metrics = rank_metrics(ranking, query.grades, k=k)
            totals[mode].append(metrics)
            rows.append({"query_id": query.id, "mode": mode, "ranking": ranking[:k], **metrics})
            if mode == "hybrid" and reranker:
                scores = await reranker.score(
                    tuple((request.normalized_query, texts[item]) for item in ids)
                )
                if len(scores) != len(ids) or any(not math.isfinite(score) for score in scores):
                    raise ValueError("Invalid reranker scores.")
                ranking = [
                    labels[ids[index]]
                    for index in sorted(range(len(ids)), key=lambda i: (-scores[i], i))
                ]
                metrics = rank_metrics(ranking, query.grades, k=k)
                totals["reranked"].append(metrics)
                rows.append(
                    {"query_id": query.id, "mode": "reranked", "ranking": ranking[:k], **metrics}
                )
    return {
        "dataset": dataset.name,
        "dataset_sha256": hashlib.sha256(dataset.model_dump_json().encode()).hexdigest(),
        "documents": len(points),
        "queries": len(dataset.queries),
        "k": k,
        "candidate_window": len(points),
        "dense_model": config.dense.model_identity,
        "sparse_model": config.sparse.model_identity,
        "reranker": reranker.identity if reranker else None,
        "scope": "Authored development regression fixture; not held-out enterprise quality.",
        "aggregate": {
            mode: {
                metric: sum(row[metric] for row in values) / len(values)
                for metric in ("recall", "mrr", "ndcg")
            }
            for mode, values in totals.items()
        },
        "per_query": rows,
    }
