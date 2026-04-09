"""
Redis cache service.

Cache key strategy: sha256(diff) → JSON-serialized list of issue dicts.
Skips redundant LLM calls for PRs whose diff hasn't changed.
"""

import hashlib
import json
import os
from typing import Any, Optional

import redis.asyncio as aioredis

CACHE_TTL_SECONDS = 60 * 60 * 24  # 24 hours

_redis_client: aioredis.Redis | None = None


def _get_client() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        _redis_client = aioredis.from_url(redis_url, decode_responses=True)
    return _redis_client


def _diff_hash(diff: str) -> str:
    """Return a hex SHA-256 digest of the diff string."""
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


async def get_cached_review(diff_hash: str) -> Optional[list[dict[str, Any]]]:
    """
    Check if this diff was already reviewed.

    Args:
        diff_hash: Pre-computed SHA-256 hex digest of the diff.

    Returns:
        Cached list of issue dicts, or None on cache miss.
    """
    client = _get_client()
    key    = f"devscan:diff:{diff_hash}"
    value  = await client.get(key)
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


async def cache_review(diff_hash: str, issues: list[dict[str, Any]]) -> None:
    """
    Cache the review results for a given diff hash.

    Args:
        diff_hash: SHA-256 hex digest of the diff.
        issues:    List of issue dicts to cache.
    """
    client = _get_client()
    key    = f"devscan:diff:{diff_hash}"
    await client.set(key, json.dumps(issues), ex=CACHE_TTL_SECONDS)


def compute_diff_hash(diff: str) -> str:
    """Public helper — compute the hash used as the cache key."""
    return _diff_hash(diff)


async def close() -> None:
    """Close the Redis connection pool on shutdown."""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
