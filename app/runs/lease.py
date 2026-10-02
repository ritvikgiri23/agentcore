"""The heartbeat lease that says a worker is executing a run.

A Redis key with a TTL, holding the holder's token. A worker takes it before claiming a
run and keeps refreshing it while the run executes, so a run that is `running` with no
lease has lost its worker. Only the holder can refresh or release it: a worker that
stalled past the TTL can't clobber the lease of whoever took over.
"""

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from redis.asyncio import Redis
from redis.asyncio.client import Pipeline
from redis.exceptions import WatchError


def lease_key(run_id: str) -> str:
    return f"run:{run_id}:lease"


class RunLease:
    """One worker's hold, or attempted hold, on one run's lease."""

    def __init__(self, redis: Redis, run_id: str, ttl_seconds: int) -> None:
        self._redis = redis
        self._key = lease_key(run_id)
        self._ttl = ttl_seconds
        self._token = uuid4().hex

    async def acquire(self) -> bool:
        """Take the lease; False if anyone holds it."""
        return bool(await self._redis.set(self._key, self._token, nx=True, ex=self._ttl))

    async def refresh(self) -> bool:
        """Extend the lease; False if it lapsed or someone else holds it."""
        return await self._if_held(lambda pipe: pipe.expire(self._key, self._ttl))

    async def release(self) -> None:
        await self._if_held(lambda pipe: pipe.delete(self._key))

    async def remaining_seconds(self) -> float:
        """How long until the current lease, whoever holds it, expires; 0 if none."""
        remaining_ms: int = await self._redis.pttl(self._key)
        return max(remaining_ms, 0) / 1000

    async def _if_held(self, write: Callable[[Pipeline], Any]) -> bool:
        # Check-and-set under WATCH: the write is dropped if the key changes in between.
        async with self._redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(self._key)
                if await pipe.get(self._key) != self._token:
                    return False
                pipe.multi()  # type: ignore[no-untyped-call]
                write(pipe)
                await pipe.execute()
            except WatchError:
                return False
        return True
