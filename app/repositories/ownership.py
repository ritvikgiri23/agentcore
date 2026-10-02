"""Lookups of resources owned by a user.

The owner is always part of the query, so another user's resource is indistinguishable
from one that does not exist: both raise NotFoundError.
"""

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models import AgentRun, AgentSession


def _canonical_id(resource_id: str) -> str | None:
    """Ids are uuid4 strings; anything else cannot match, so it never reaches the query."""
    try:
        return str(uuid.UUID(resource_id))
    except ValueError:
        return None


async def get_owned_session(db: AsyncSession, user_id: str, session_id: str) -> AgentSession:
    canonical_id = _canonical_id(session_id)
    agent_session = None
    if canonical_id is not None:
        agent_session = await db.scalar(
            select(AgentSession).where(
                AgentSession.id == canonical_id, AgentSession.user_id == user_id
            )
        )
    if agent_session is None:
        raise NotFoundError("Session not found")
    structlog.contextvars.bind_contextvars(session_id=agent_session.id)
    return agent_session


async def get_owned_run(db: AsyncSession, user_id: str, run_id: str) -> AgentRun:
    """Runs have no user id of their own; ownership is checked through their session."""
    canonical_id = _canonical_id(run_id)
    run = None
    if canonical_id is not None:
        run = await db.scalar(
            select(AgentRun)
            .join(AgentSession, AgentRun.session_id == AgentSession.id)
            .where(AgentRun.id == canonical_id, AgentSession.user_id == user_id)
        )
    if run is None:
        raise NotFoundError("Run not found")
    structlog.contextvars.bind_contextvars(run_id=run.id, session_id=run.session_id)
    return run
