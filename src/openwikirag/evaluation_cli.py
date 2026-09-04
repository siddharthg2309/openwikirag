"""Run a read-only labeled ablation: python -m openwikirag.evaluation_cli."""

import argparse
import asyncio
import json
from pathlib import Path

from openwikirag.application.evaluation import EvaluationDataset, evaluate
from openwikirag.infrastructure.reranking import SentenceTransformersReranker


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("evals/retrieval.json"))
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--model")
    parser.add_argument("--revision")
    args = parser.parse_args()
    if bool(args.model) != bool(args.revision):
        parser.error("--model and --revision must be supplied together")
    provider = (
        SentenceTransformersReranker(model=args.model, revision=args.revision)
        if args.model
        else None
    )
    dataset = EvaluationDataset.model_validate_json(args.dataset.read_bytes())
    print(json.dumps(asyncio.run(evaluate(dataset, k=args.k, reranker=provider)), indent=2))


if __name__ == "__main__":
    main()
