"""Bounded in-process cache for loaded embedding model/provider instances."""

import asyncio
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar

RepresentationKind = Literal["dense", "sparse"]
ModelT = TypeVar("ModelT")
_CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")

MAX_MODEL_CACHE_ENTRIES = 32


class ModelCacheError(Exception):
    """Base error for model-cache lifecycle and identity failures."""


class ModelCacheConfigurationError(ModelCacheError):
    """Raised when cache capacity is invalid."""


class ModelCacheInputError(ModelCacheError):
    """Raised when a cache key or model value is invalid."""


class ModelCacheClosedError(ModelCacheError):
    """Raised when callers use a cache after shutdown."""


@dataclass(frozen=True, slots=True)
class ModelCacheKey:
    """Identity for one loaded representation model, excluding request data."""

    representation: RepresentationKind
    provider_identity: str
    model_identity: str
    configuration_checksum_sha256: str

    def __post_init__(self) -> None:
        if self.representation not in {"dense", "sparse"}:
            raise ModelCacheInputError("Model-cache representation must be dense or sparse.")
        if not isinstance(self.provider_identity, str) or not self.provider_identity.strip():
            raise ModelCacheInputError("Model-cache provider identity cannot be empty.")
        if not isinstance(self.model_identity, str) or not self.model_identity.strip():
            raise ModelCacheInputError("Model-cache model identity cannot be empty.")
        if not isinstance(self.configuration_checksum_sha256, str) or not (
            _CHECKSUM_PATTERN.fullmatch(self.configuration_checksum_sha256)
        ):
            raise ModelCacheInputError("Model-cache configuration checksum is invalid.")


@dataclass(frozen=True, slots=True)
class ModelCacheConfig:
    """Server-owned capacity for one worker-process model cache."""

    max_entries: int = 4

    def __post_init__(self) -> None:
        if (
            type(self.max_entries) is not int
            or not 1 <= self.max_entries <= MAX_MODEL_CACHE_ENTRIES
        ):
            raise ModelCacheConfigurationError(
                f"Model-cache capacity must be between 1 and {MAX_MODEL_CACHE_ENTRIES}."
            )


class ModelLoader(Protocol[ModelT]):
    """Provider-owned construction and cleanup boundary for loaded models."""

    async def load(self, key: ModelCacheKey) -> ModelT:
        """Load one model/provider instance for an exact cache identity."""

    async def close(self, model: ModelT) -> None:
        """Release resources held by one loaded model/provider instance."""


class ModelCacheLoadError(ModelCacheError):
    """Raised when a model load fails or returns no model."""

    def __init__(self, *, key: ModelCacheKey, message: str) -> None:
        self.key = key
        super().__init__(f"Model load failed for {key.model_identity}: {message}")


class ModelCacheCleanupError(ModelCacheError):
    """Raised when an evicted or retained model cannot be closed."""

    def __init__(self, *, key: ModelCacheKey, message: str) -> None:
        self.key = key
        super().__init__(f"Model cleanup failed for {key.model_identity}: {message}")


class EmbeddingModelCache[ModelT]:
    """Cache loaded model instances with LRU and single-flight semantics."""

    def __init__(
        self,
        loader: ModelLoader[ModelT],
        *,
        config: ModelCacheConfig | None = None,
    ) -> None:
        if not callable(getattr(loader, "load", None)) or not callable(
            getattr(loader, "close", None)
        ):
            raise ModelCacheInputError("Model cache requires a loader with load and close methods.")
        self._loader = loader
        self._config = config or ModelCacheConfig()
        self._values: OrderedDict[ModelCacheKey, ModelT] = OrderedDict()
        self._inflight: dict[ModelCacheKey, asyncio.Task[ModelT]] = {}
        self._closed = False

    async def get_or_load(self, key: ModelCacheKey) -> ModelT:
        """Return a cached model or share one in-flight load for this key."""

        self._validate_key(key)
        if key in self._values:
            model = self._values.pop(key)
            self._values[key] = model
            return model

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                self._load_and_store(key),
                name=f"model-cache-load-{key.representation}-{key.model_identity}",
            )
            self._inflight[key] = task
            task.add_done_callback(
                lambda completed: self._on_load_done(key, completed),
            )
        return await asyncio.shield(task)

    async def close(self) -> None:
        """Cancel in-flight loads and close all retained models."""

        if self._closed and not self._values and not self._inflight:
            return
        self._closed = True

        tasks = list(self._inflight.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()

        retained = list(self._values.items())
        self._values.clear()
        cleanup_failures: list[tuple[ModelCacheKey, Exception]] = []
        for key, model in retained:
            try:
                await self._loader.close(model)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                cleanup_failures.append((key, exc))

        if cleanup_failures:
            key, failure = cleanup_failures[0]
            raise ModelCacheCleanupError(
                key=key,
                message=str(failure) or failure.__class__.__name__,
            ) from failure

    async def _load_and_store(self, key: ModelCacheKey) -> ModelT:
        try:
            model = await self._loader.load(key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ModelCacheLoadError(
                key=key,
                message=str(exc) or exc.__class__.__name__,
            ) from exc

        if model is None:
            raise ModelCacheLoadError(key=key, message="loader returned no model")
        if self._closed:
            await self._close_value(key, model)
            raise ModelCacheClosedError("Model cache closed while loading a model.")

        self._values[key] = model
        self._values.move_to_end(key)
        while len(self._values) > self._config.max_entries:
            evicted_key, evicted_model = self._values.popitem(last=False)
            await self._close_value(evicted_key, evicted_model)
        return model

    async def _close_value(self, key: ModelCacheKey, model: ModelT) -> None:
        try:
            await self._loader.close(model)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ModelCacheCleanupError(
                key=key,
                message=str(exc) or exc.__class__.__name__,
            ) from exc

    def _validate_key(self, key: ModelCacheKey) -> None:
        if not isinstance(key, ModelCacheKey):
            raise ModelCacheInputError("Model cache requires a ModelCacheKey.")
        if self._closed:
            raise ModelCacheClosedError("Model cache is closed.")

    def _on_load_done(
        self,
        key: ModelCacheKey,
        task: asyncio.Future[ModelT],
    ) -> None:
        if not task.cancelled():
            task.exception()
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)


__all__ = [
    "EmbeddingModelCache",
    "MAX_MODEL_CACHE_ENTRIES",
    "ModelCacheCleanupError",
    "ModelCacheClosedError",
    "ModelCacheConfig",
    "ModelCacheConfigurationError",
    "ModelCacheError",
    "ModelCacheInputError",
    "ModelCacheKey",
    "ModelCacheLoadError",
    "ModelLoader",
    "RepresentationKind",
]
