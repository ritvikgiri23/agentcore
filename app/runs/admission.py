"""Per-user cap on active (queued or running) runs.

Each user has a Redis sorted set of their active run ids, scored by admission time. A
run is admitted before it is inserted, atomically and only while the set is below the
limit, and released when it reaches any terminal state. If the set drifts (a release
lost to a crash, say), a full set is reconciled against Postgres before a submission is
turned away, so stale ids never lock a user out.
"""

import time

import structlog
from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ACTIVE_STATUSES, AgentRun, AgentSession, RunStatus

logger = structlog.get_logger(__name__)

_ADMIT_SCRIPT = """
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[2]) then
    redis.call('ZADD', KEYS[1], ARGV[3], ARGV[1])
    return 1
end
return 0
"""

# An admitted run is inserted right after admission; until then, its id is not in
# Postgres yet and must not be mistaken for a stale one.
INSERT_GRACE_SECONDS = 60


def active_runs_key(user_id: str) -> str:
    return f"user:{user_id}:active_runs"


async def admit(
    db: AsyncSession, redis: Redis, user_id: str, run_id: str, *, limit: int
) -> bool:
    """Take an active-run slot for a run about to be inserted; False if the user is full."""
    if await _try_admit(redis, user_id, run_id, limit):
        return True
    if not await _reconcile(db, redis, user_id):
        return False
    return await _try_admit(redis, user_id, run_id, limit)


async def release(redis: Redis, user_id: str, run_id: str) -> None:
    """Free the run's slot. Idempotent."""
    await redis.zrem(active_runs_key(user_id), run_id)


async def _try_admit(redis: Redis, user_id: str, run_id: str, limit: int) -> bool:
    admitted = await redis.eval(  # type: ignore[misc]
        _ADMIT_SCRIPT, 1, active_runs_key(user_id), run_id, str(limit), str(time.time())
    )
    return bool(admitted)


async def _reconcile(db: AsyncSession, redis: Redis, user_id: str) -> bool:
    """Make the set match the user's active runs in Postgres. True if it shrank."""
    key = active_runs_key(user_id)
    members: dict[str, float] = dict(await redis.zrange(key, 0, -1, withscores=True))
    rows = await db.execute(
        select(AgentRun.id, AgentRun.status)
        .join(AgentSession, AgentSession.id == AgentRun.session_id)
        .where(
            AgentSession.user_id == user_id,
            or_(AgentRun.id.in_(members), AgentRun.status.in_(ACTIVE_STATUSES)),
        )
    )
    statuses: dict[str, RunStatus] = {run_id: status for run_id, status in rows}
    inserted_before = time.time() - INSERT_GRACE_SECONDS
    stale = [
        run_id
        for run_id, admitted_at in members.items()
        if _is_stale(statuses.get(run_id), admitted_at, inserted_before)
    ]
    missing = {
        run_id: time.time()
        for run_id, status in statuses.items()
        if status in ACTIVE_STATUSES and run_id not in members
    }
    if not stale and not missing:
        return False
    async with redis.pipeline(transaction=True) as pipe:
        if stale:
            pipe.zrem(key, *stale)
        if missing:
            # NX: never move an existing member's admission time.
            pipe.zadd(key, missing, nx=True)
        await pipe.execute()
    logger.warning(
        "active_runs_reconciled", user_id=user_id, dropped=len(stale), added=len(missing)
    )
    return len(stale) > len(missing)


def _is_stale(status: RunStatus | None, admitted_at: float, inserted_before: float) -> bool:
    if status is None:
        # Deleted, or never inserted; or admitted a moment ago and about to be.
        return admitted_at < inserted_before
    return status not in ACTIVE_STATUSES
