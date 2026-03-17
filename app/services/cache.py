"""
Redis cache service for code review results.

Cache key strategy: sha256(code) → review_id (int)
This lets us skip re-analysis for identical code submissions.
"""

import hashlib
import json
import os
from typing import Optional

import redis.asyncio as aioredis

CACHE_TTL_SECONDS = 60 * 60 * 24  # 24 hours

_redis_client: aioredis.Redis | None = None


def _get_client() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        _redis_client = aioredis.from_url(redis_url, decode_responses=True)
    return _redis_client


def _code_hash(code: str) -> str:
    """Return a hex SHA-256 digest of the submitted code string."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


async def get_cached_review(code: str) -> Optional[int]:
    """
    Look up a cached review ID for the given code.

    Returns:
        The review ID (int) if a cache hit exists, otherwise None.
    """
    client = _get_client()
    key = f"devscan:review:{_code_hash(code)}"
    value = await client.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def cache_review(code: str, review_id: int) -> None:
    """
    Store a review_id in Redis keyed by the hash of the code.

    Args:
        code:      The source code that was reviewed.
        review_id: The DB id of the resulting Review record.
    """
    client = _get_client()
    key = f"devscan:review:{_code_hash(code)}"
    await client.set(key, str(review_id), ex=CACHE_TTL_SECONDS)


async def invalidate_cached_review(code: str) -> None:
    """Remove a cached review entry (e.g. if re-analysis is forced)."""
    client = _get_client()
    key = f"devscan:review:{_code_hash(code)}"
    await client.delete(key)


async def close() -> None:
    """Close the Redis connection pool gracefully."""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
