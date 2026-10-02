"""The only place a run's status changes.

Every transition is a conditional UPDATE, so concurrent writers (a duplicate delivery,
a cancellation, a worker finishing up) cannot both win, and a terminal status is never
overwritten. Whichever writer wins a terminal transition frees the run's active-run slot.
"""

from collections.abc import Collection

import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import ScalarSelect

from app.models import ACTIVE_STATUSES, AgentRun, AgentSession, RunStatus
from app.runs import admission

logger = structlog.get_logger(__name__)


async def claim(db: AsyncSession, run_id: str) -> AgentRun | None:
    """Atomically move a queued run to running; None if it was not queued."""
    run = await db.scalar(
        update(AgentRun)
        .where(AgentRun.id == run_id, AgentRun.status == RunStatus.QUEUED)
        .values(status=RunStatus.RUNNING, started_at=func.clock_timestamp())
        .returning(AgentRun)
    )
    await db.commit()
    return run


async def get_status(db: AsyncSession, run_id: str) -> RunStatus | None:
    """The run's current status, read fresh; None if the run doesn't exist."""
    return await db.scalar(select(AgentRun.status).where(AgentRun.id == run_id))


async def set_tokens_used(db: AsyncSession, run_id: str, tokens_used: int) -> None:
    """Record progress on a running run; a terminal run's row is never touched."""
    await db.execute(
        update(AgentRun)
        .where(AgentRun.id == run_id, AgentRun.status == RunStatus.RUNNING)
        .values(tokens_used=tokens_used)
    )
    await db.commit()


async def complete(
    db: AsyncSession, redis: Redis, run_id: str, *, final_answer: str, tokens_used: int
) -> bool:
    """running → completed. False if the run had already left the running state."""
    return await _finish(
        db,
        redis,
        run_id,
        RunStatus.COMPLETED,
        from_statuses=(RunStatus.RUNNING,),
        final_answer=final_answer,
        tokens_used=tokens_used,
    )


async def fail(
    db: AsyncSession, redis: Redis, run_id: str, *, tokens_used: int | None = None
) -> bool:
    """queued|running → failed. False if the run was already terminal."""
    values = {} if tokens_used is None else {"tokens_used": tokens_used}
    return await _finish(
        db, redis, run_id, RunStatus.FAILED, from_statuses=ACTIVE_STATUSES, **values
    )


async def cancel(db: AsyncSession, redis: Redis, run_id: str) -> RunStatus | None:
    """queued|running → cancelled. Returns the status it left; None if it was not active."""
    # The locked subquery reads the status this update replaces; a racing claim waits,
    # then finds the run no longer queued.
    old = (
        select(AgentRun.id, AgentRun.status)
        .where(AgentRun.id == run_id)
        .with_for_update()
        .subquery()
    )
    cancelled = (
        await db.execute(
            update(AgentRun)
            .where(AgentRun.id == old.c.id, old.c.status.in_(ACTIVE_STATUSES))
            .values(status=RunStatus.CANCELLED, finished_at=func.clock_timestamp())
            .returning(old.c.status, _owner_id())
        )
    ).one_or_none()
    await db.commit()
    if cancelled is None:
        return None
    previous, user_id = cancelled
    await _release(redis, user_id, run_id)
    return RunStatus(previous)


async def _finish(
    db: AsyncSession,
    redis: Redis,
    run_id: str,
    status: RunStatus,
    *,
    from_statuses: Collection[RunStatus],
    **values: object,
) -> bool:
    finished = (
        await db.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status.in_(from_statuses))
            .values(status=status, finished_at=func.clock_timestamp(), **values)
            .returning(_owner_id())
        )
    ).one_or_none()
    await db.commit()
    if finished is None:
        return False
    await _release(redis, finished[0], run_id)
    return True


def _owner_id() -> ScalarSelect[str]:
    """The id of the user owning the run being updated, for a RETURNING clause."""
    return (
        select(AgentSession.user_id)
        .where(AgentSession.id == AgentRun.session_id)
        .scalar_subquery()
    )


async def _release(redis: Redis, user_id: str, run_id: str) -> None:
    try:
        await admission.release(redis, user_id, run_id)
    except Exception:
        # The transition stands; the stale slot is reconciled when the user next needs it.
        logger.exception("active_run_slot_not_released", run_id=run_id)
