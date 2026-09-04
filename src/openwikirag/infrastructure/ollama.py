"""Ollama JSON-schema adapter with digest verification and bounded HTTP reads."""

import json
import re
from typing import Any

import httpx

from openwikirag.application.answers import (
    AnswerContext,
    DraftAnswer,
    GenerationOutputError,
    GenerationRetryableError,
)


class OllamaAnswerProvider:
    def __init__(
        self,
        *,
        model: str,
        digest: str,
        base_url: str = "http://127.0.0.1:11434",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not model.strip() or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Ollama requires a model name and expected immutable digest.")
        self.model, self.digest, self.base_url, self.transport = model, digest, base_url, transport
        self.last_usage: dict[str, int] = {}

    @property
    def identity(self) -> str:
        return (
            f"ollama:{self.model}:expected-digest={self.digest}:pre-post-checked:"
            "quotes-v1:ctx8192:out1024:t0"
        )

    async def generate(self, context: AnswerContext) -> DraftAnswer:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=60,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                tags = await self._read(client, "GET", "/api/tags", limit=65536)
                if not any(
                    item.get("name") == self.model and item.get("digest") == self.digest
                    for item in tags.get("models", [])
                ):
                    raise GenerationOutputError(
                        "Configured model is unavailable or its digest changed."
                    )
                result = await self._read(
                    client,
                    "POST",
                    "/api/chat",
                    limit=131072,
                    payload={
                        "model": self.model,
                        "messages": context.messages(),
                        "format": DraftAnswer.model_json_schema(),
                        "stream": False,
                        "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 1024},
                    },
                )
                if (
                    result.get("model") != self.model
                    or result.get("done") is not True
                    or result.get("message", {}).get("tool_calls")
                ):
                    raise GenerationOutputError("Model response identity or completion is invalid.")
                # The API targets a mutable name. Detect drift, but do not claim
                # atomic digest pinning (an administrator can still cause an ABA race).
                after = await self._read(client, "GET", "/api/tags", limit=65536)
                if not any(
                    item.get("name") == self.model and item.get("digest") == self.digest
                    for item in after.get("models", [])
                ):
                    raise GenerationOutputError("Model digest changed during inference.")
                self.last_usage = {
                    key: result[key]
                    for key in ("prompt_eval_count", "eval_count", "total_duration")
                    if type(result.get(key)) is int
                }
                return DraftAnswer.model_validate_json(result["message"]["content"])
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise GenerationRetryableError("The local model dependency is unavailable.") from exc
        except (GenerationOutputError, GenerationRetryableError):
            raise
        except Exception as exc:
            raise GenerationOutputError("The model response is malformed.") from exc

    @staticmethod
    async def _read(
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        limit: int,
        payload: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        async with client.stream(method, path, json=payload) as response:
            if response.status_code == 429 or response.status_code >= 500:
                raise GenerationRetryableError("The local model is temporarily unavailable.")
            if response.status_code != 200:
                raise GenerationOutputError("The model request was rejected.")
            data = bytearray()
            async for part in response.aiter_bytes():
                data.extend(part)
                if len(data) > limit:
                    raise GenerationOutputError("Model response exceeds the byte limit.")
            value = json.loads(data)
            if not isinstance(value, dict):
                raise GenerationOutputError("Expected a JSON object from the model.")
            return value
