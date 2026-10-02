"""The only place a run's status changes.

Every transition is a conditional UPDATE, so concurrent writers (a duplicate delivery,
a cancellation, a worker finishing up) cannot both win, and a terminal status is never
overwritten.
"""

from collections.abc import Collection

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun, RunStatus

_ACTIVE = (RunStatus.QUEUED, RunStatus.RUNNING)


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


async def set_tokens_used(db: AsyncSession, run_id: str, tokens_used: int) -> None:
    """Record progress on a running run; a terminal run's row is never touched."""
    await db.execute(
        update(AgentRun)
        .where(AgentRun.id == run_id, AgentRun.status == RunStatus.RUNNING)
        .values(tokens_used=tokens_used)
    )
    await db.commit()


async def complete(db: AsyncSession, run_id: str, *, final_answer: str, tokens_used: int) -> bool:
    """running → completed. False if the run had already left the running state."""
    return await _finish(
        db,
        run_id,
        RunStatus.COMPLETED,
        from_statuses=(RunStatus.RUNNING,),
        final_answer=final_answer,
        tokens_used=tokens_used,
    )


async def fail(db: AsyncSession, run_id: str, *, tokens_used: int | None = None) -> bool:
    """queued|running → failed. False if the run was already terminal."""
    values = {} if tokens_used is None else {"tokens_used": tokens_used}
    return await _finish(db, run_id, RunStatus.FAILED, from_statuses=_ACTIVE, **values)


async def _finish(
    db: AsyncSession,
    run_id: str,
    status: RunStatus,
    *,
    from_statuses: Collection[RunStatus],
    **values: object,
) -> bool:
    finished_id = await db.scalar(
        update(AgentRun)
        .where(AgentRun.id == run_id, AgentRun.status.in_(from_statuses))
        .values(status=status, finished_at=func.clock_timestamp(), **values)
        .returning(AgentRun.id)
    )
    await db.commit()
    return finished_id is not None
