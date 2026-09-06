"""Run a read-only labeled ablation: python -m openwikirag.evaluation_cli."""

import argparse
import asyncio
import json
from pathlib import Path

from openwikirag.application.embeddings import (
    DenseEmbeddingProvider,
    EmbeddingConfig,
    EmbeddingError,
)
from openwikirag.application.evaluation import EvaluationDataset, evaluate
from openwikirag.infrastructure.ollama_embeddings import OllamaDenseEmbeddingProvider
from openwikirag.infrastructure.reranking import SentenceTransformersReranker


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("evals/retrieval.json"))
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--model")
    parser.add_argument("--revision")
    parser.add_argument("--dense-model")
    parser.add_argument("--dense-digest")
    parser.add_argument("--dense-base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--dense-dimensions", type=int, default=128)
    parser.add_argument("--dense-timeout-seconds", type=float, default=60.0)
    args = parser.parse_args()
    if bool(args.model) != bool(args.revision):
        parser.error("--model and --revision must be supplied together")
    if bool(args.dense_model) != bool(args.dense_digest):
        parser.error("--dense-model and --dense-digest must be supplied together")
    provider = (
        SentenceTransformersReranker(model=args.model, revision=args.revision)
        if args.model
        else None
    )
    try:
        dense_provider: DenseEmbeddingProvider | None = None
        dense_config: EmbeddingConfig | None = None
        if args.dense_model:
            dense_config = EmbeddingConfig(
                model_identity=f"{args.dense_model}@{args.dense_digest}",
                dimensions=args.dense_dimensions,
            )
            dense_provider = OllamaDenseEmbeddingProvider(
                model=args.dense_model,
                digest=args.dense_digest,
                base_url=args.dense_base_url,
                timeout_seconds=args.dense_timeout_seconds,
            )
        dataset = EvaluationDataset.model_validate_json(args.dataset.read_bytes())
        report = asyncio.run(
            evaluate(
                dataset,
                k=args.k,
                reranker=provider,
                dense_provider=dense_provider,
                dense_config=dense_config,
            )
        )
    except (ValueError, EmbeddingError) as exc:
        parser.error(f"evaluation failed: {exc}")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
