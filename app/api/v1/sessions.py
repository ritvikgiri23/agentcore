from typing import Any

import structlog
from fastapi import APIRouter, Response, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, PageParams
from app.core.errors import ErrorResponse, UnknownToolError
from app.models import AgentSession
from app.repositories.ownership import get_owned_session
from app.schemas.pagination import Page
from app.schemas.sessions import SessionCreate, SessionDetail, SessionResponse
from app.tools import list_tool_names

router = APIRouter(prefix="/sessions", tags=["sessions"])
logger = structlog.get_logger(__name__)

_UNAUTHORIZED: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token"},
}
_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Session not found"},
}


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
    # Runs arrive with the run pipeline; until then every session has none.
    return SessionDetail(
        **SessionResponse.model_validate(agent_session).model_dump(), recent_runs=[]
    )


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete an agent session",
    responses={**_UNAUTHORIZED, **_NOT_FOUND},
)
async def delete_session(session_id: str, user: CurrentUser, db: DbSession) -> None:
    agent_session = await get_owned_session(db, user.id, session_id)
    await db.delete(agent_session)
    await db.commit()
    logger.info("session_deleted")
