"""Bounded, order-preserving orchestration for embedding providers."""

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from itertools import islice
from typing import Protocol, TypeVar

RequestT = TypeVar("RequestT")
ResultT = TypeVar("ResultT")
ProviderRequestT = TypeVar("ProviderRequestT", contravariant=True)
ProviderResultT = TypeVar("ProviderResultT", covariant=True)

MAX_EMBEDDING_BATCH_SIZE = 512
MAX_EMBEDDING_CONCURRENCY = 64


class EmbeddingBatchError(Exception):
    """Base error for bounded embedding orchestration."""


class EmbeddingBatchConfigurationError(EmbeddingBatchError):
    """Raised when batch limits cannot provide bounded execution."""


class EmbeddingBatchInputError(EmbeddingBatchError):
    """Raised when the request collection cannot be materialized."""


class _IndexedEmbeddingBatchError(EmbeddingBatchError):
    """Base class for failures associated with one request position."""

    def __init__(self, *, index: int, message: str) -> None:
        self.index = index
        super().__init__(f"Embedding request at index {index} failed: {message}")


class EmbeddingBatchProviderError(_IndexedEmbeddingBatchError):
    """Raised when a provider call fails for one request."""


class EmbeddingBatchOutputError(_IndexedEmbeddingBatchError):
    """Raised when result validation fails for one request."""


@dataclass(frozen=True, slots=True)
class EmbeddingBatchConfig:
    """Server-owned limits for one bounded embedding batch operation."""

    max_batch_size: int = 32
    max_concurrency: int = 4

    def __post_init__(self) -> None:
        if (
            type(self.max_batch_size) is not int
            or not 1 <= self.max_batch_size <= MAX_EMBEDDING_BATCH_SIZE
        ):
            raise EmbeddingBatchConfigurationError(
                f"Maximum batch size must be between 1 and {MAX_EMBEDDING_BATCH_SIZE}."
            )
        if (
            type(self.max_concurrency) is not int
            or not 1 <= self.max_concurrency <= MAX_EMBEDDING_CONCURRENCY
        ):
            raise EmbeddingBatchConfigurationError(
                f"Maximum concurrency must be between 1 and {MAX_EMBEDDING_CONCURRENCY}."
            )
        if self.max_concurrency > self.max_batch_size:
            raise EmbeddingBatchConfigurationError(
                "Maximum concurrency cannot exceed maximum batch size."
            )


class EmbeddingBatchProvider(Protocol[ProviderRequestT, ProviderResultT]):
    """Application-owned provider port used by the batch scheduler."""

    async def embed(self, request: ProviderRequestT) -> ProviderResultT:
        """Produce one provider result for one request."""


EmbeddingResultValidator = Callable[[RequestT, ResultT], ResultT]


class EmbeddingBatcher[RequestT, ResultT]:
    """Run provider calls in bounded sequential batches and input order."""

    def __init__(
        self,
        provider: EmbeddingBatchProvider[RequestT, ResultT],
        *,
        config: EmbeddingBatchConfig | None = None,
        validate_result: EmbeddingResultValidator[RequestT, ResultT] | None = None,
    ) -> None:
        self._provider = provider
        self._config = config or EmbeddingBatchConfig()
        self._validate_result = validate_result

    async def embed(self, requests: Iterable[RequestT]) -> tuple[ResultT, ...]:
        """Return every validated result in request order or raise without partial output."""

        try:
            request_iterator = iter(requests)
        except TypeError as exc:
            raise EmbeddingBatchInputError(
                "Embedding requests must be an iterable collection."
            ) from exc

        results: list[ResultT] = []
        offset = 0
        while batch := tuple(islice(request_iterator, self._config.max_batch_size)):
            start = offset
            results.extend(await self._run_batch(batch=batch, offset=start))
            offset += len(batch)
        return tuple(results)

    async def _run_batch(
        self,
        *,
        batch: tuple[RequestT, ...],
        offset: int,
    ) -> tuple[ResultT, ...]:
        semaphore = asyncio.Semaphore(self._config.max_concurrency)

        async def run_one(local_index: int, request: RequestT) -> ResultT:
            async with semaphore:
                try:
                    result = await self._provider.embed(request)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise EmbeddingBatchProviderError(
                        index=offset + local_index,
                        message=str(exc) or exc.__class__.__name__,
                    ) from exc

            if self._validate_result is None:
                return result

            try:
                return self._validate_result(request, result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise EmbeddingBatchOutputError(
                    index=offset + local_index,
                    message=str(exc) or exc.__class__.__name__,
                ) from exc

        tasks = [
            asyncio.create_task(
                run_one(local_index, request),
                name=f"embedding-batch-{offset + local_index}",
            )
            for local_index, request in enumerate(batch)
        ]

        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            failures = [
                (index, task)
                for index, task in enumerate(tasks)
                if task in done and not task.cancelled() and task.exception() is not None
            ]
            if failures:
                _, first_failure = min(failures, key=lambda item: item[0])
                first_failure.result()

            return tuple(task.result() for task in tasks)
        except BaseException:
            await self._cancel_tasks(tasks)
            raise

    @staticmethod
    async def _cancel_tasks(tasks: list[asyncio.Task[ResultT]]) -> None:
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


__all__ = [
    "EmbeddingBatchConfig",
    "EmbeddingBatchConfigurationError",
    "EmbeddingBatchError",
    "EmbeddingBatchInputError",
    "EmbeddingBatchOutputError",
    "EmbeddingBatchProvider",
    "EmbeddingBatchProviderError",
    "EmbeddingBatcher",
    "EmbeddingResultValidator",
    "MAX_EMBEDDING_BATCH_SIZE",
    "MAX_EMBEDDING_CONCURRENCY",
]
