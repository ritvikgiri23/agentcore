import asyncio
import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Response, status
from redis.asyncio import Redis
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, Enqueuer, PageParams, RedisClient, Revoker
from app.api.v1.runs import status_url, stream_url
from app.core.config import get_settings
from app.core.errors import (
    ErrorResponse,
    RateLimitedError,
    ServiceUnavailableError,
    UnknownToolError,
)
from app.memory import short_term
from app.models import AgentRun, AgentSession, RunStatus
from app.repositories.ownership import get_owned_session
from app.core.redis import get_redis
from app.runs import admission, cancellation, lifecycle
from app.runs.events import publish_done
from app.schemas.pagination import Page
from app.schemas.runs import RunAccepted, RunCreate
from app.schemas.sessions import RunSummary, SessionCreate, SessionDetail, SessionResponse
from app.tools import list_tool_names

router = APIRouter(prefix="/sessions", tags=["sessions"])
logger = structlog.get_logger(__name__)

_UNAUTHORIZED: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token"},
}
_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Session not found"},
}

RECENT_RUNS_LIMIT = 5
RUN_SUMMARY_MESSAGE_CHARS = 120


@router.post(
    "",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an agent session",
    responses={
        **_UNAUTHORIZED,
        422: {"model": ErrorResponse, "description": "Invalid fields or unknown tools"},
    },
)
async def create_session(body: SessionCreate, user: CurrentUser, db: DbSession) -> SessionResponse:
    available = list_tool_names()
    unknown = [name for name in body.tools_enabled if name not in available]
    if unknown:
        raise UnknownToolError(
            f"Unknown tools: {', '.join(unknown)}",
            details={"unknown": unknown, "available": available},
        )
    agent_session = AgentSession(
        user_id=user.id,
        name=body.name,
        system_prompt=body.system_prompt,
        tools_enabled=body.tools_enabled,
    )
    db.add(agent_session)
    await db.commit()
    await db.refresh(agent_session)
    structlog.contextvars.bind_contextvars(session_id=agent_session.id)
    logger.info("session_created")
    return SessionResponse.model_validate(agent_session)


@router.get(
    "",
    response_model=Page[SessionResponse],
    status_code=status.HTTP_200_OK,
    summary="List your agent sessions, newest first",
    responses={
        **_UNAUTHORIZED,
        422: {"model": ErrorResponse, "description": "Invalid limit or offset"},
    },
)
async def list_sessions(
    user: CurrentUser, db: DbSession, paging: PageParams
) -> Page[SessionResponse]:
    owned = AgentSession.user_id == user.id
    total = await db.scalar(select(func.count()).select_from(AgentSession).where(owned))
    agent_sessions = await db.scalars(
        select(AgentSession)
        .where(owned)
        .order_by(AgentSession.created_at.desc(), AgentSession.id.desc())
        .limit(paging.limit)
        .offset(paging.offset)
    )
    return Page(
        items=[SessionResponse.model_validate(s) for s in agent_sessions],
        total=total or 0,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.get(
    "/{session_id}",
    response_model=SessionDetail,
    status_code=status.HTTP_200_OK,
    summary="Get an agent session with its most recent runs",
    responses={**_UNAUTHORIZED, **_NOT_FOUND},
)
async def get_session(session_id: str, user: CurrentUser, db: DbSession) -> SessionDetail:
    agent_session = await get_owned_session(db, user.id, session_id)
    recent_runs = await db.scalars(
        select(AgentRun)
        .where(AgentRun.session_id == agent_session.id)
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(RECENT_RUNS_LIMIT)
    )
    return SessionDetail(
        **SessionResponse.model_validate(agent_session).model_dump(),
        recent_runs=[
            RunSummary(
                id=run.id,
                status=run.status,
                message=run.user_message[:RUN_SUMMARY_MESSAGE_CHARS],
                tokens_used=run.tokens_used,
                created_at=run.created_at,
                finished_at=run.finished_at,
            )
            for run in recent_runs
        ],
    )


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete an agent session, cancelling its queued and running runs first",
    responses={**_UNAUTHORIZED, **_NOT_FOUND},
)
async def delete_session(
    session_id: str,
    user: CurrentUser,
    db: DbSession,
    redis: RedisClient,
    revoke: Revoker,
) -> None:
    agent_session = await get_owned_session(db, user.id, session_id)
    # Before the delete, so no worker keeps executing a run whose session is gone.
    await cancellation.cancel_session_runs(db, redis, agent_session.id, revoke=revoke)
    await db.delete(agent_session)
    await db.commit()
    # Long-term memories are the user's, not the session's, so they stay.
    await short_term.clear(redis, session_id)
    logger.info("session_deleted")


@router.post(
    "/{session_id}/run",
    response_model=RunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a message to a session; the agent runs it in the background",
    responses={
        **_UNAUTHORIZED,
        **_NOT_FOUND,
        422: {"model": ErrorResponse, "description": "Invalid message"},
        429: {"model": ErrorResponse, "description": "Too many queued or running runs"},
        503: {"model": ErrorResponse, "description": "The run could not be queued"},
    },
)
async def submit_run(
    session_id: str,
    body: RunCreate,
    user: CurrentUser,
    db: DbSession,
    redis: Annotated[Redis, Depends(get_redis)],
    enqueue: Enqueuer,
) -> RunAccepted:
    agent_session = await get_owned_session(db, user.id, session_id)
    run_id = str(uuid.uuid4())
    limit = get_settings().max_active_runs_per_user
    if not await admission.admit(db, redis, user.id, run_id, limit=limit):
        logger.info("run_rate_limited", limit=limit)
        raise RateLimitedError(
            f"At most {limit} runs can be queued or running at once; wait for one to finish",
            details={"limit": limit},
        )
    run = AgentRun(id=run_id, session_id=agent_session.id, user_message=body.message)
    db.add(run)
    try:
        # Committed before enqueueing, so the worker can always find the run.
        await db.commit()
    except Exception:
        await admission.release(redis, user.id, run_id)
        raise
    structlog.contextvars.bind_contextvars(run_id=run.id)
    try:
        # Publishing to the broker is blocking network I/O.
        await asyncio.to_thread(enqueue, run.id)
    except Exception:
        logger.exception("run_enqueue_failed")
        if await lifecycle.fail(db, redis, run.id):
            await publish_done(redis, run.id, RunStatus.FAILED)
        raise ServiceUnavailableError(
            "The run could not be queued; try again", details={"run_id": run.id}
        ) from None
    logger.info("run_submitted")
    return RunAccepted(
        run_id=run.id,
        status=RunStatus.QUEUED,
        status_url=status_url(run.id),
        stream_url=stream_url(run.id),
    )
