"""Proof for bounded single-flight model/provider caching."""

import asyncio
from dataclasses import dataclass, field

import pytest

from openwikirag.application.model_cache import (
    EmbeddingModelCache,
    ModelCacheCleanupError,
    ModelCacheClosedError,
    ModelCacheConfig,
    ModelCacheConfigurationError,
    ModelCacheInputError,
    ModelCacheKey,
    ModelCacheLoadError,
)


@dataclass(frozen=True, slots=True)
class FakeModel:
    name: str


@dataclass
class FakeLoader:
    load_calls: list[ModelCacheKey] = field(default_factory=list)
    close_calls: list[FakeModel] = field(default_factory=list)
    failures_remaining: dict[ModelCacheKey, int] = field(default_factory=dict)
    close_failures: set[str] = field(default_factory=set)
    gate: asyncio.Event | None = None
    started: asyncio.Event | None = None
    cancelled: asyncio.Event | None = None

    async def load(self, key: ModelCacheKey) -> FakeModel:
        self.load_calls.append(key)
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                if self.cancelled is not None:
                    self.cancelled.set()
                raise
        remaining = self.failures_remaining.get(key, 0)
        if remaining:
            self.failures_remaining[key] = remaining - 1
            raise RuntimeError("loader unavailable")
        return FakeModel(name=f"{key.representation}:{key.model_identity}")

    async def close(self, model: FakeModel) -> None:
        self.close_calls.append(model)
        if model.name in self.close_failures:
            raise RuntimeError("close unavailable")


def _key(
    *,
    representation: str = "dense",
    provider: str = "provider-a",
    model: str = "model-a",
    checksum: str = "a" * 64,
) -> ModelCacheKey:
    return ModelCacheKey(
        representation=representation,  # type: ignore[arg-type]
        provider_identity=provider,
        model_identity=model,
        configuration_checksum_sha256=checksum,
    )


async def test_cache_hit_reuses_one_loaded_instance() -> None:
    loader = FakeLoader()
    cache = EmbeddingModelCache(loader)
    key = _key()

    first = await cache.get_or_load(key)
    second = await cache.get_or_load(key)

    assert first is second
    assert loader.load_calls == [key]
    await cache.close()
    assert loader.close_calls == [first]


async def test_concurrent_same_key_loads_once_and_shares_result() -> None:
    gate = asyncio.Event()
    started = asyncio.Event()
    loader = FakeLoader(gate=gate, started=started)
    cache = EmbeddingModelCache(loader)
    key = _key()

    first_task = asyncio.create_task(cache.get_or_load(key))
    await started.wait()
    second_task = asyncio.create_task(cache.get_or_load(key))
    gate.set()

    first, second = await asyncio.gather(first_task, second_task)

    assert first is second
    assert len(loader.load_calls) == 1
    await cache.close()


async def test_representation_provider_model_and_configuration_identity_do_not_collide() -> None:
    loader = FakeLoader()
    cache = EmbeddingModelCache(loader, config=ModelCacheConfig(max_entries=4))
    keys = (
        _key(representation="dense"),
        _key(representation="sparse"),
        _key(provider="provider-b"),
        _key(model="model-b"),
        _key(checksum="b" * 64),
    )

    results = [await cache.get_or_load(key) for key in keys]

    assert len(loader.load_calls) == len(keys)
    assert len({id(result) for result in results}) == len(keys)
    await cache.close()


async def test_failed_load_is_not_cached() -> None:
    key = _key()
    loader = FakeLoader(failures_remaining={key: 1})
    cache = EmbeddingModelCache(loader)

    with pytest.raises(ModelCacheLoadError):
        await cache.get_or_load(key)
    result = await cache.get_or_load(key)

    assert result.name == "dense:model-a"
    assert loader.load_calls == [key, key]
    await cache.close()


async def test_lru_eviction_closes_least_recently_used_model() -> None:
    loader = FakeLoader()
    cache = EmbeddingModelCache(loader, config=ModelCacheConfig(max_entries=2))
    first_key = _key(model="model-1")
    second_key = _key(model="model-2")
    third_key = _key(model="model-3")

    first = await cache.get_or_load(first_key)
    second = await cache.get_or_load(second_key)
    assert await cache.get_or_load(first_key) is first
    third = await cache.get_or_load(third_key)

    assert second in loader.close_calls
    assert third not in loader.close_calls
    assert await cache.get_or_load(second_key) is not second
    await cache.close()


async def test_cancelling_one_waiter_does_not_cancel_shared_load() -> None:
    gate = asyncio.Event()
    started = asyncio.Event()
    loader = FakeLoader(gate=gate, started=started)
    cache = EmbeddingModelCache(loader)
    key = _key()

    owner = asyncio.create_task(cache.get_or_load(key))
    await started.wait()
    waiter = asyncio.create_task(cache.get_or_load(key))
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    gate.set()
    result = await owner
    assert result.name == "dense:model-a"
    assert len(loader.load_calls) == 1
    await cache.close()


async def test_close_cancels_inflight_load_and_rejects_future_access() -> None:
    key = _key()
    gate = asyncio.Event()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    loader = FakeLoader(
        gate=gate,
        started=started,
        cancelled=cancelled,
    )
    cache = EmbeddingModelCache(loader)
    loading = asyncio.create_task(cache.get_or_load(key))
    await started.wait()

    await cache.close()

    with pytest.raises(asyncio.CancelledError):
        await loading
    assert cancelled.is_set()
    with pytest.raises(ModelCacheClosedError):
        await cache.get_or_load(key)
    await cache.close()


async def test_cleanup_failure_is_typed_and_cache_remains_consistent() -> None:
    loader = FakeLoader(close_failures={"dense:model-1"})
    cache = EmbeddingModelCache(loader, config=ModelCacheConfig(max_entries=1))
    first_key = _key(model="model-1")
    second_key = _key(model="model-2")

    first = await cache.get_or_load(first_key)
    with pytest.raises(ModelCacheCleanupError):
        await cache.get_or_load(second_key)

    assert first in loader.close_calls
    assert (await cache.get_or_load(second_key)).name == "dense:model-2"
    await cache.close()


def test_invalid_cache_configuration_and_keys_fail_closed() -> None:
    with pytest.raises(ModelCacheConfigurationError):
        ModelCacheConfig(max_entries=0)
    with pytest.raises(ModelCacheConfigurationError):
        ModelCacheConfig(max_entries=33)
    with pytest.raises(ModelCacheConfigurationError):
        ModelCacheConfig(max_entries=True)
    with pytest.raises(ModelCacheInputError):
        _key(checksum="not-a-checksum")
    with pytest.raises(ModelCacheInputError):
        ModelCacheKey(
            representation="graph",  # type: ignore[arg-type]
            provider_identity="provider",
            model_identity="model",
            configuration_checksum_sha256="a" * 64,
        )


async def test_loader_returning_none_is_not_cached() -> None:
    key = _key()

    @dataclass
    class EmptyLoader:
        load_calls: list[ModelCacheKey] = field(default_factory=list)

        async def load(self, key: ModelCacheKey) -> None:
            self.load_calls.append(key)
            return None

        async def close(self, model: None) -> None:
            return None

    loader = EmptyLoader()
    cache = EmbeddingModelCache[None](loader)

    with pytest.raises(ModelCacheLoadError):
        await cache.get_or_load(key)
    with pytest.raises(ModelCacheLoadError):
        await cache.get_or_load(key)
    assert loader.load_calls == [key, key]
