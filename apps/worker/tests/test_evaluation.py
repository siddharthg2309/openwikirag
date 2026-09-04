from pathlib import Path

import pytest

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
