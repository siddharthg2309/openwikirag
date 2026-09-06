"""Contract tests for the bounded local performance benchmark."""

import json
import math

import pytest

from openwikirag.benchmark_cli import (
    BenchmarkConfig,
    BenchmarkInputError,
    _percentile,
    main,
    run_benchmark,
)


def test_percentile_interpolates_and_rejects_empty_samples() -> None:
    assert _percentile((1.0, 2.0, 3.0, 4.0), 0.5) == 2.5
    with pytest.raises(BenchmarkInputError):
        _percentile((), 0.5)


def test_workload_limits_fail_closed() -> None:
    with pytest.raises(BenchmarkInputError):
        BenchmarkConfig(iterations=0)
    with pytest.raises(BenchmarkInputError):
        BenchmarkConfig(documents=501)
    with pytest.raises(BenchmarkInputError):
        BenchmarkConfig(queries=21)
    with pytest.raises(BenchmarkInputError):
        BenchmarkConfig(concurrency=33)


@pytest.mark.asyncio
async def test_benchmark_reports_all_modes_and_finite_statistics() -> None:
    report = await run_benchmark(
        BenchmarkConfig(documents=2, queries=2, warmup=1, iterations=2, concurrency=3)
    )
    assert report["schema_version"] == "benchmark-v1"
    workload = report["workload"]
    assert isinstance(workload, dict)
    assert workload["vector_points"] == 4
    stages = report["stages"]
    assert isinstance(stages, dict)
    assert set(stages) == {"dense", "sparse", "hybrid"}
    for mode in stages.values():
        assert isinstance(mode, dict)
        for summary in mode.values():
            assert isinstance(summary, dict)
            assert summary["sample_count"] == 2
            assert summary["operations_per_sample"] == 3
            assert summary["operations_per_second"] > 0
            assert summary["p95"] >= summary["p50"]
            assert all(
                math.isfinite(float(summary[key]))
                for key in ("min", "mean", "p50", "p95", "max")
            )


def test_cli_emits_machine_readable_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(
            [
                "--documents",
                "1",
                "--queries",
                "1",
                "--warmup",
                "0",
                "--iterations",
                "1",
                "--concurrency",
                "2",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["workload"]["documents"] == 1
    assert report["workload"]["measured_iterations"] == 1
    assert report["workload"]["concurrency"] == 2


def test_cli_rejects_invalid_limits() -> None:
    with pytest.raises(SystemExit):
        main(["--iterations", "0"])
