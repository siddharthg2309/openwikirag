import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from openwikirag.infrastructure.streams import RedisStreamPublisher
from openwikirag.security.rate_limit import RedisFixedWindowLimiter


@pytest.mark.asyncio
async def test_real_redis_stream_publisher_writes_event() -> None:
    redis_url = os.environ.get("OPENWIKIRAG_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("Set OPENWIKIRAG_TEST_REDIS_URL to run Redis integration tests.")

    stream_name = f"openwikirag:test:{uuid4()}"
    client = Redis.from_url(redis_url, decode_responses=True)
    publisher = RedisStreamPublisher(client)
    try:
        message_id = await publisher.publish(
            stream_name=stream_name,
            event_id=str(uuid4()),
            event_type="document.ingestion.requested",
            tenant_id=str(uuid4()),
            aggregate_id=str(uuid4()),
            payload={"document_version_id": str(uuid4()), "pipeline_version": "ingestion-v1"},
        )
        messages = await client.xrange(stream_name)
    finally:
        await client.delete(stream_name)
        await client.aclose()

    assert len(messages) == 1
    assert messages[0][0] == message_id
    assert messages[0][1]["event_type"] == "document.ingestion.requested"


@pytest.mark.asyncio
async def test_real_redis_consumer_group_reads_and_acknowledges() -> None:
    redis_url = os.environ.get("OPENWIKIRAG_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("Set OPENWIKIRAG_TEST_REDIS_URL to run Redis integration tests.")

    stream_name = f"openwikirag:test:{uuid4()}"
    group_name = f"group-{uuid4()}"
    client = Redis.from_url(redis_url, decode_responses=True)
    publisher = RedisStreamPublisher(client)
    try:
        await publisher.publish(
            stream_name=stream_name,
            event_id=str(uuid4()),
            event_type="document.ingestion.requested",
            tenant_id=str(uuid4()),
            aggregate_id=str(uuid4()),
            payload={"ingestion_job_id": str(uuid4())},
        )
        await publisher.ensure_group(stream_name=stream_name, group_name=group_name)
        messages = await publisher.read(
            stream_name=stream_name,
            group_name=group_name,
            consumer_name="consumer-1",
            count=1,
            block_ms=100,
        )
        assert len(messages) == 1
        await publisher.acknowledge(
            stream_name=stream_name,
            group_name=group_name,
            message_id=messages[0].message_id,
        )
        pending = await client.xpending(stream_name, group_name)
    finally:
        await client.delete(stream_name)
        await client.aclose()

    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_real_redis_rate_limiter_shares_one_fixed_window() -> None:
    redis_url = os.environ.get("OPENWIKIRAG_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("Set OPENWIKIRAG_TEST_REDIS_URL to run Redis integration tests.")

    redis = Redis.from_url(redis_url, decode_responses=True)
    limiter = RedisFixedWindowLimiter(redis)
    peer = f"integration-{uuid4()}"
    key = limiter.key(route="/api/v1/auth/token", peer=peer)
    try:
        first = await limiter.check(
            route="/api/v1/auth/token",
            peer=peer,
            limit=1,
            window_seconds=30,
        )
        second = await limiter.check(
            route="/api/v1/auth/token",
            peer=peer,
            limit=1,
            window_seconds=30,
        )
    finally:
        await redis.delete(key)
        await redis.aclose()

    assert first.allowed
    assert first.remaining == 0
    assert not second.allowed
    assert second.retry_after_seconds >= 1
