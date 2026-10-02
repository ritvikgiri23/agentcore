"""Cancelling a run: the request, the flag its worker checks, and the outcome it ends with.

A request moves the run to cancelled, then sets a Redis flag holding the reason. A run
nobody is executing is settled on the spot; a worker executing one stops at its next
step boundary when it sees the flag, or is interrupted by the revoke if it is blocked
inside a call, and settles the run itself, so its `cancelled` step is its last.
Settling is idempotent: every cancelled run ends with one `cancelled` step and `done`.
"""

import asyncio
import enum
from collections.abc import Callable

import structlog
from redis.asyncio import Redis
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ACTIVE_STATUSES, AgentRun, RunStatus, RunStep, StepType
from app.runs import lifecycle
from app.runs.events import publish_done, record_step
from app.runs.lease import lease_key

logger = structlog.get_logger(__name__)


class CancelReason(enum.StrEnum):
    """Why a run was cancelled; the `cancelled` step's `reason`."""

    BY_USER = "cancelled by user"
    SESSION_DELETED = "session deleted"
    # A run stopped with no request on record, e.g. by a revoke from outside the API.
    UNKNOWN = "cancelled"


# Long enough to outlive any run; the flag only matters while one executes.
CANCEL_FLAG_TTL_SECONDS = 24 * 60 * 60

type RunRevoker = Callable[[str], None]


def cancel_flag_key(run_id: str) -> str:
    return f"run:{run_id}:cancel"


async def request_cancel(
    db: AsyncSession, redis: Redis, run_id: str, *, reason: CancelReason, revoke: RunRevoker
) -> RunStatus | None:
    """Cancel a queued or running run. Returns the status it left; None if it was not active.

    `revoke` is blocking: it runs in a worker thread.
    """
    previous = await lifecycle.cancel(db, run_id)
    if previous is None:
        return None
    await redis.set(cancel_flag_key(run_id), reason.value, ex=CANCEL_FLAG_TTL_SECONDS)
    # A queued run's claim now fails, and a running run with no lease has lost its
    # worker: either way nobody else will record the outcome.
    if previous == RunStatus.QUEUED or not await redis.exists(lease_key(run_id)):
        await settle(db, redis, run_id)
    try:
        # Drops a queued task before it starts; interrupts one blocked inside a call.
        await asyncio.to_thread(revoke, run_id)
    except Exception:
        # The flag alone still stops the worker, at its next step boundary.
        logger.exception("run_revoke_failed", run_id=run_id)
    logger.info("run_cancel_requested", run_id=run_id, previous_status=previous, reason=reason)
    return previous


async def cancel_session_runs(
    db: AsyncSession, redis: Redis, session_id: str, *, revoke: RunRevoker
) -> None:
    """Cancel every queued or running run of a session that is about to be deleted."""
    run_ids = list(
        await db.scalars(
            select(AgentRun.id).where(
                AgentRun.session_id == session_id, AgentRun.status.in_(ACTIVE_STATUSES)
            )
        )
    )
    for run_id in run_ids:
        await request_cancel(
            db, redis, run_id, reason=CancelReason.SESSION_DELETED, revoke=revoke
        )


async def is_requested(redis: Redis, run_id: str) -> bool:
    return bool(await redis.exists(cancel_flag_key(run_id)))


async def settle(db: AsyncSession, redis: Redis, run_id: str) -> None:
    """End a run being stopped for cancellation: cancelled, one `cancelled` step, `done`.

    Cancels the run if it is still active. A run that finished first is left alone; one
    deleted with its session only gets its `done`, for anyone still watching. `done` may
    be published more than once; a stream ends at the first.
    """
    await lifecycle.cancel(db, run_id)
    status = await lifecycle.get_status(db, run_id)
    if status is None:
        logger.info("run_deleted", run_id=run_id)
    elif status != RunStatus.CANCELLED:
        logger.info("run_cancellation_skipped", run_id=run_id, status=status)
        return
    elif await _lock_uncancelled(db, run_id):
        reason = await redis.get(cancel_flag_key(run_id)) or CancelReason.UNKNOWN
        # Commits, releasing the lock.
        await record_step(db, redis, run_id, StepType.CANCELLED, {"reason": reason})
    else:
        await db.commit()
    await publish_done(redis, run_id, RunStatus.CANCELLED)


async def _lock_uncancelled(db: AsyncSession, run_id: str) -> bool:
    """Lock the run if it still lacks its `cancelled` step, so only one settle records it.

    False if the step exists or the run was deleted meanwhile.
    """
    locked = await db.scalar(select(AgentRun.id).where(AgentRun.id == run_id).with_for_update())
    if locked is None:
        return False
    # A separate statement, so it sees a step committed by a settle that held the lock first.
    has_step = await db.scalar(
        select(
            exists().where(
                RunStep.run_id == run_id, RunStep.step_type == StepType.CANCELLED.value
            )
        )
    )
    return not has_step
