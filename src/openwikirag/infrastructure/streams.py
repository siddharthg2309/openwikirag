"""Redis Streams transport adapter used by the outbox publisher."""

import json
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from redis.typing import FieldT


class StreamPublisher(Protocol):
    """Small transport port so publisher behavior can be tested without Redis."""

    async def publish(
        self,
        *,
        stream_name: str,
        event_id: str,
        event_type: str,
        tenant_id: str,
        aggregate_id: str,
        payload: dict[str, object],
        traceparent: str | None = None,
    ) -> str:
        """Append an event and return the Redis stream message id."""


@dataclass(frozen=True, slots=True)
class StreamMessage:
    message_id: str
    fields: dict[str, str]


class StreamTransport(StreamPublisher, Protocol):
    """Redis Streams operations needed by the ingestion consumer."""

    async def ensure_group(self, *, stream_name: str, group_name: str) -> None:
        """Create a consumer group if it does not already exist."""

    async def read(
        self,
        *,
        stream_name: str,
        group_name: str,
        consumer_name: str,
        count: int,
        block_ms: int,
    ) -> list[StreamMessage]:
        """Read new messages for one consumer."""

    async def claim_stale(
        self,
        *,
        stream_name: str,
        group_name: str,
        consumer_name: str,
        min_idle_ms: int,
        count: int,
    ) -> list[StreamMessage]:
        """Transfer stale pending messages to this consumer."""

    async def acknowledge(
        self,
        *,
        stream_name: str,
        group_name: str,
        message_id: str,
    ) -> None:
        """Acknowledge a message after its durable outcome is recorded."""

    async def publish_dead_letter(
        self,
        *,
        stream_name: str,
        message: StreamMessage,
        reason: str,
    ) -> str:
        """Copy an unprocessable message to the dead-letter stream."""


class RedisStreamPublisher:
    """Append and consume serialized events through Redis Streams."""

    def __init__(self, client: Redis) -> None:
        self._client = client

    @classmethod
    def from_url(cls, redis_url: str) -> "RedisStreamPublisher":
        return cls(Redis.from_url(redis_url, decode_responses=True))

    async def publish(
        self,
        *,
        stream_name: str,
        event_id: str,
        event_type: str,
        tenant_id: str,
        aggregate_id: str,
        payload: dict[str, object],
        traceparent: str | None = None,
    ) -> str:
        fields: dict[FieldT, FieldT] = {
            "event_id": event_id,
            "event_type": event_type,
            "tenant_id": tenant_id,
            "aggregate_id": aggregate_id,
            "payload": json.dumps(payload, separators=(",", ":"), sort_keys=True),
        }
        if traceparent is not None:
            fields["traceparent"] = traceparent
        message_id = await self._client.xadd(stream_name, fields)
        return str(message_id)

    async def ensure_group(self, *, stream_name: str, group_name: str) -> None:
        try:
            await self._client.xgroup_create(
                name=stream_name,
                groupname=group_name,
                id="0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read(
        self,
        *,
        stream_name: str,
        group_name: str,
        consumer_name: str,
        count: int,
        block_ms: int,
    ) -> list[StreamMessage]:
        entries = await self._client.xreadgroup(
            groupname=group_name,
            consumername=consumer_name,
            streams={stream_name: ">"},
            count=count,
            block=block_ms,
        )
        return _flatten_stream_entries(entries)

    async def claim_stale(
        self,
        *,
        stream_name: str,
        group_name: str,
        consumer_name: str,
        min_idle_ms: int,
        count: int,
    ) -> list[StreamMessage]:
        result = await self._client.xautoclaim(
            stream_name,
            group_name,
            consumer_name,
            min_idle_time=min_idle_ms,
            start_id="0-0",
            count=count,
        )
        return _flatten_message_entries(result[1])

    async def acknowledge(
        self,
        *,
        stream_name: str,
        group_name: str,
        message_id: str,
    ) -> None:
        await self._client.xack(stream_name, group_name, message_id)

    async def publish_dead_letter(
        self,
        *,
        stream_name: str,
        message: StreamMessage,
        reason: str,
    ) -> str:
        message_id = await self._client.xadd(
            stream_name,
            {
                "original_message_id": message.message_id,
                "reason": reason,
                "event": json.dumps(message.fields, separators=(",", ":"), sort_keys=True),
            },
        )
        return str(message_id)

    async def close(self) -> None:
        await self._client.aclose()


def _flatten_stream_entries(entries: object) -> list[StreamMessage]:
    if not isinstance(entries, list):
        return []
    messages: list[StreamMessage] = []
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        raw_messages = entry[1]
        if isinstance(raw_messages, list):
            messages.extend(_flatten_message_entries(raw_messages))
    return messages


def _flatten_message_entries(entries: object) -> list[StreamMessage]:
    if not isinstance(entries, list):
        return []
    messages: list[StreamMessage] = []
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        message_id, raw_fields = entry
        if not isinstance(raw_fields, dict):
            continue
        messages.append(
            StreamMessage(
                message_id=str(message_id),
                fields={str(key): str(value) for key, value in raw_fields.items()},
            )
        )
    return messages
