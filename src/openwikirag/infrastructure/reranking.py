"""Optional local Sentence Transformers cross-encoder with explicit CPU ownership."""

import asyncio
import importlib
import re
from typing import Any

from openwikirag.application.reranking import RerankingProviderError


class SentenceTransformersReranker:
    """One lazy model and one active inference task per API process instance."""

    def __init__(self, *, model: str, revision: str, allow_download: bool = False) -> None:
        if not model.strip() or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("Cross-encoder requires a model and immutable 40-character revision.")
        self._name = model
        self._revision = revision
        self._allow_download = allow_download
        self._model: Any = None
        self._active: asyncio.Task[tuple[float, ...]] | None = None

    @property
    def identity(self) -> str:
        return f"sentence-transformers:{self._name}@{self._revision}:cpu:max_length=512"

    async def score(self, pairs: tuple[tuple[str, str], ...]) -> tuple[float, ...]:
        if len(pairs) > 100 or any(len(a) > 4096 or len(b) > 32000 for a, b in pairs):
            raise RerankingProviderError("Cross-encoder input exceeds its bounds.")
        if not pairs:
            return ()
        if self._active is not None and not self._active.done():
            raise RerankingProviderError("Cross-encoder is busy; retry later.")
        task = asyncio.create_task(asyncio.to_thread(self._predict, pairs))
        self._active = task
        task.add_done_callback(self._observe_completion)
        # Cancellation ends the caller's wait, not a still-running CPU operation.
        return await asyncio.shield(task)

    @staticmethod
    def _observe_completion(task: asyncio.Task[tuple[float, ...]]) -> None:
        if not task.cancelled():
            task.exception()  # Retrieve failures even if the HTTP waiter timed out.

    def _predict(self, pairs: tuple[tuple[str, str], ...]) -> tuple[float, ...]:
        if self._model is None:
            library = importlib.import_module("sentence_transformers")
            self._model = library.CrossEncoder(
                self._name,
                revision=self._revision,
                device="cpu",
                max_length=512,
                trust_remote_code=False,
                local_files_only=not self._allow_download,
            )
        values = self._model.predict(list(pairs), batch_size=8, show_progress_bar=False)
        return tuple(float(value) for value in values)
