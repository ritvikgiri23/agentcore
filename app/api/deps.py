from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, Query
from fastapi.security import OAuth2PasswordBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import UnauthorizedError
from app.core.redis import get_redis
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models import User
from app.schemas.pagination import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, MAX_PAGE_OFFSET
from app.worker.tasks import enqueue_run

# auto_error=False so a missing token is rendered by our own 401 envelope.
_bearer = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


async def get_current_user(
    token: Annotated[str | None, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Authenticate the request from its `Authorization: Bearer` header."""
    return await _authenticate(token, db)


async def get_current_user_sse(
    token: Annotated[str | None, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
    access_token: Annotated[
        str | None,
        Query(description="Access token, for EventSource clients that cannot send headers"),
    ] = None,
) -> User:
    """Like `get_current_user`, but also accepts an `access_token` query parameter.

    Only the stream route uses this: query strings end up in logs and browser history.
    The request log scrubs the parameter.
    """
    return await _authenticate(token or access_token, db)


async def _authenticate(token: str | None, db: AsyncSession) -> User:
    if not token:
        raise UnauthorizedError()
    user = await db.get(User, decode_access_token(token))
    if user is None:
        raise UnauthorizedError("Invalid access token")
    structlog.contextvars.bind_contextvars(user_id=user.id)
    return user


@dataclass(frozen=True)
class Pagination:
    limit: int
    offset: int


def get_pagination(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_PAGE_OFFSET)] = 0,
) -> Pagination:
    return Pagination(limit=limit, offset=offset)


type RunEnqueuer = Callable[[str], None]


def get_run_enqueuer() -> RunEnqueuer:
    """Hands a run id to the broker. Overridden in tests, which never run a broker."""
    return enqueue_run


CurrentUser = Annotated[User, Depends(get_current_user)]
StreamUser = Annotated[User, Depends(get_current_user_sse)]
DbSession = Annotated[AsyncSession, Depends(get_db)]
RedisClient = Annotated[Redis, Depends(get_redis)]
PageParams = Annotated[Pagination, Depends(get_pagination)]
Enqueuer = Annotated[RunEnqueuer, Depends(get_run_enqueuer)]
