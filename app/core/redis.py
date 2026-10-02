from collections.abc import AsyncIterator
from functools import lru_cache

from redis.asyncio import Redis

from app.core.config import get_settings


@lru_cache
def get_redis_client() -> Redis:
    client: Redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return client


async def get_redis() -> AsyncIterator[Redis]:
    """FastAPI dependency yielding the shared async Redis client."""
    yield get_redis_client()
