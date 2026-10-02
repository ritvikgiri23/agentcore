import json
from collections.abc import AsyncIterator
from typing import Annotated, Any, NamedTuple

import structlog
from fastapi import APIRouter, Depends, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from redis.asyncio.client import PubSub
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentUser,
    DbSession,
    PageParams,
    RedisClient,
    Revoker,
    StreamUser,
)
from app.api.v1 import API_V1_PREFIX
from app.core.errors import ConflictError, ErrorResponse
from app.models import AgentRun, RunStatus, RunStep
from app.repositories.ownership import get_owned_run
from app.runs import cancellation
from app.runs.stream import run_events, subscribe
from app.schemas.pagination import Page
from app.schemas.runs import RunStatusResponse, RunStepResponse

router = APIRouter(prefix="/runs", tags=["runs"])
logger = structlog.get_logger(__name__)

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token"},
    404: {"model": ErrorResponse, "description": "Run not found"},
}


def status_url(run_id: str) -> str:
    return f"{API_V1_PREFIX}{router.prefix}/{run_id}/status"


def stream_url(run_id: str) -> str:
    return f"{API_V1_PREFIX}{router.prefix}/{run_id}/stream"


@router.get(
    "/{run_id}/status",
    response_model=RunStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get a run's status, token usage and, once completed, its final answer",
    responses=_ERRORS,
)
async def get_run_status(run_id: str, user: CurrentUser, db: DbSession) -> RunStatusResponse:
    run = await get_owned_run(db, user.id, run_id)
    return _status_response(run, await _count_steps(db, run.id))


@router.delete(
    "/{run_id}",
    response_model=RunStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Cancel a queued or running run",
    description=(
        "A queued run never starts. A running run stops at its next step boundary, or "
        "is interrupted if it is blocked inside a call. Either way it ends with status "
        "`cancelled`, a `cancelled` step and a `done` event; the response already shows "
        "the new status."
    ),
    responses={
        **_ERRORS,
        409: {"model": ErrorResponse, "description": "The run has already finished"},
    },
)
async def cancel_run(
    run_id: str, user: CurrentUser, db: DbSession, redis: RedisClient, revoke: Revoker
) -> RunStatusResponse:
    run = await get_owned_run(db, user.id, run_id)
    cancelled_from = await cancellation.request_cancel(
        db, redis, run.id, reason=cancellation.CancelReason.BY_USER, revoke=revoke
    )
    await db.refresh(run)
    if cancelled_from is None:
        raise ConflictError(
            f"Run is already {run.status.value}", details={"status": run.status.value}
        )
    logger.info("run_cancelled_by_user")
    return _status_response(run, await _count_steps(db, run.id))


async def _count_steps(db: AsyncSession, run_id: str) -> int:
    count = await db.scalar(
        select(func.count()).select_from(RunStep).where(RunStep.run_id == run_id)
    )
    return count or 0


def _status_response(run: AgentRun, step_count: int) -> RunStatusResponse:
    return RunStatusResponse(
        run_id=run.id,
        status=run.status,
        tokens_used=run.tokens_used,
        step_count=step_count,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        final_answer=run.final_answer if run.status == RunStatus.COMPLETED else None,
    )


@router.get(
    "/{run_id}/steps",
    response_model=Page[RunStepResponse],
    status_code=status.HTTP_200_OK,
    summary="List a run's steps in the order they happened",
    responses={
        **_ERRORS,
        422: {"model": ErrorResponse, "description": "Invalid limit or offset"},
    },
)
async def list_run_steps(
    run_id: str, user: CurrentUser, db: DbSession, paging: PageParams
) -> Page[RunStepResponse]:
    run = await get_owned_run(db, user.id, run_id)
    total = await _count_steps(db, run.id)
    steps = await db.scalars(
        select(RunStep)
        .where(RunStep.run_id == run.id)
        .order_by(RunStep.occurred_at, RunStep.id)
        .limit(paging.limit)
        .offset(paging.offset)
    )
    return Page(
        items=[RunStepResponse.model_validate(s) for s in steps],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )


class _RunSubscription(NamedTuple):
    run_id: str
    pubsub: PubSub


async def _subscribed_run(
    run_id: str, user: StreamUser, db: DbSession, redis: RedisClient
) -> AsyncIterator[_RunSubscription]:
    # A dependency, not part of the stream body: once streaming starts the 200 is already
    # sent, so ownership must be settled first. The subscription is closed when the
    # request ends, whether the run finished or the client went away.
    run = await get_owned_run(db, user.id, run_id)
    async with subscribe(redis, run.id) as pubsub:
        yield _RunSubscription(run.id, pubsub)


@router.get(
    "/{run_id}/stream",
    response_class=EventSourceResponse,
    status_code=status.HTTP_200_OK,
    summary="Stream a run's steps live as Server-Sent Events, ending with a done event",
    description=(
        "Replays every persisted step, then relays new ones as they happen, each exactly "
        "once. The last event is `{\"step_type\": \"done\", \"status\": ...}` with the "
        "run's terminal status, after which the stream closes. Idle streams receive "
        "keepalive comments. Accepts the token as a Bearer header or, for EventSource "
        "clients, an `access_token` query parameter."
    ),
    responses=_ERRORS,
)
async def stream_run(
    subscription: Annotated[_RunSubscription, Depends(_subscribed_run)], db: DbSession
) -> AsyncIterator[ServerSentEvent]:
    async for event in run_events(db, subscription.pubsub, subscription.run_id):
        yield ServerSentEvent(raw_data=json.dumps(event))
