"""
Redis Pub/Sub service for real-time review status broadcasting.

Each review gets its own channel: devscan:review:<id>:status
The pipeline publishes to this channel after every status transition.
The GraphQL subscription resolver listens on it and streams updates to clients.

Note: pub/sub requires a dedicated connection (can't share with regular commands),
so this module manages its own connections rather than using the shared client
from cache.py.
"""

import json
import os
from typing import AsyncGenerator, Any

import redis.asyncio as aioredis

# Statuses that mean the review is finished — subscription stops here
TERMINAL_STATUSES = frozenset({"POSTED", "FAILED"})


def _redis_url() -> str:
    return os.getenv("REDIS_URL", "redis://localhost:6379")


async def publish_status_update(review_id: int, status: str) -> None:
    """
    Publish a review status change to its Redis channel.
    Called by the pipeline after every commit that changes the status.
    """
    channel = f"devscan:review:{review_id}:status"
    payload = json.dumps({"review_id": review_id, "status": status})

    async with aioredis.from_url(_redis_url(), decode_responses=True) as client:
        await client.publish(channel, payload)


async def subscribe_review_status(
    review_id: int,
) -> AsyncGenerator[dict[str, Any], None]:
    """
    Async generator that yields status-update dicts as the review progresses.

    Yields one dict per transition:
        {"review_id": int, "status": "PENDING" | "ANALYZING" | "POSTED" | "FAILED"}

    Stops automatically once a terminal status (POSTED or FAILED) is received,
    or if the channel publishes nothing for TIMEOUT seconds.
    """
    channel = f"devscan:review:{review_id}:status"

    async with aioredis.from_url(_redis_url(), decode_responses=True) as client:
        async with client.pubsub() as pubsub:
            await pubsub.subscribe(channel)

            async for raw_message in pubsub.listen():
                if raw_message["type"] != "message":
                    continue

                try:
                    data: dict = json.loads(raw_message["data"])
                except (json.JSONDecodeError, TypeError):
                    continue

                yield data

                if data.get("status") in TERMINAL_STATUSES:
                    await pubsub.unsubscribe(channel)
                    break
