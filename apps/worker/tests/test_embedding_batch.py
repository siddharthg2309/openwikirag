"""Proof for bounded embedding batch orchestration."""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from openwikirag.application.embedding_batch import (
    EmbeddingBatchConfig,
    EmbeddingBatchConfigurationError,
    EmbeddingBatcher,
    EmbeddingBatchInputError,
    EmbeddingBatchOutputError,
    EmbeddingBatchProviderError,
)


@dataclass
class RecordingProvider:
    delays: dict[int, float] = field(default_factory=dict)
    calls: list[int] = field(default_factory=list)
    active: int = 0
    max_active: int = 0

    async def embed(self, request: int) -> str:
        self.calls.append(request)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delays.get(request, 0.0))
            return f"result-{request}"
        finally:
            self.active -= 1


async def test_empty_input_does_not_call_provider() -> None:
    provider = RecordingProvider()

    result = await EmbeddingBatcher(provider).embed(())

    assert result == ()
    assert provider.calls == []


async def test_batches_preserve_order_and_bound_concurrency() -> None:
    provider = RecordingProvider(
        delays={0: 0.03, 1: 0.01, 2: 0.02, 3: 0.01, 4: 0.0},
    )
    batcher = EmbeddingBatcher(
        provider,
        config=EmbeddingBatchConfig(max_batch_size=3, max_concurrency=2),
    )

    result = await batcher.embed(range(5))

    assert result == tuple(f"result-{index}" for index in range(5))
    assert provider.calls == [0, 1, 2, 3, 4]
    assert provider.max_active == 2


def test_invalid_limits_fail_before_provider_work() -> None:
    with pytest.raises(EmbeddingBatchConfigurationError):
        EmbeddingBatchConfig(max_batch_size=0)
    with pytest.raises(EmbeddingBatchConfigurationError):
        EmbeddingBatchConfig(max_concurrency=0)
    with pytest.raises(EmbeddingBatchConfigurationError):
        EmbeddingBatchConfig(max_batch_size=2, max_concurrency=3)
    with pytest.raises(EmbeddingBatchConfigurationError):
        EmbeddingBatchConfig(max_batch_size=True)


async def test_provider_failure_contains_global_index_and_suppresses_later_batches() -> None:
    provider = RecordingProvider()
    consumed: list[int] = []

    def request_stream() -> Iterator[int]:
        for index in range(4):
            consumed.append(index)
            yield index

    async def fail(request: int) -> str:
        provider.calls.append(request)
        if request == 1:
            raise RuntimeError("provider unavailable")
        await asyncio.sleep(0)
        return f"result-{request}"

    provider.embed = fail  # type: ignore[method-assign]
    batcher = EmbeddingBatcher(
        provider,
        config=EmbeddingBatchConfig(max_batch_size=2, max_concurrency=1),
    )

    with pytest.raises(EmbeddingBatchProviderError) as error:
        await batcher.embed(request_stream())

    assert error.value.index == 1
    assert "provider unavailable" in str(error.value)
    assert provider.calls == [0, 1]
    assert consumed == [0, 1]


async def test_provider_failure_cancels_sibling_task() -> None:
    provider = RecordingProvider()
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def embed(request: int) -> str:
        provider.calls.append(request)
        if request == 0:
            raise RuntimeError("failed request")
        sibling_started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise
        return "unreachable"

    provider.embed = embed  # type: ignore[method-assign]
    batcher = EmbeddingBatcher(
        provider,
        config=EmbeddingBatchConfig(max_batch_size=2, max_concurrency=2),
    )

    task = asyncio.create_task(batcher.embed((0, 1)))
    await sibling_started.wait()
    with pytest.raises(EmbeddingBatchProviderError) as error:
        await task

    assert error.value.index == 0
    assert sibling_cancelled.is_set()


async def test_validator_failure_contains_global_index_and_returns_no_partial_result() -> None:
    provider = RecordingProvider()

    def validate(request: int, result: str) -> str:
        if request == 2:
            raise ValueError("wrong result identity")
        return result

    batcher = EmbeddingBatcher(
        provider,
        config=EmbeddingBatchConfig(max_batch_size=3, max_concurrency=2),
        validate_result=validate,
    )

    with pytest.raises(EmbeddingBatchOutputError) as error:
        await batcher.embed(range(3))

    assert error.value.index == 2
    assert "wrong result identity" in str(error.value)


async def test_caller_cancellation_cancels_and_awaits_provider_task() -> None:
    provider = RecordingProvider()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def embed(request: int) -> str:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return f"result-{request}"

    provider.embed = embed  # type: ignore[method-assign]
    task = asyncio.create_task(
        EmbeddingBatcher(
            provider,
            config=EmbeddingBatchConfig(max_batch_size=1, max_concurrency=1),
        ).embed((1,))
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


async def test_non_iterable_input_is_rejected() -> None:
    with pytest.raises(EmbeddingBatchInputError, match="iterable collection"):
        await EmbeddingBatcher(RecordingProvider()).embed(None)  # type: ignore[arg-type]
