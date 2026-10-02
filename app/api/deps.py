from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, Query
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import UnauthorizedError
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models import User
from app.schemas.pagination import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, MAX_PAGE_OFFSET

# auto_error=False so a missing token is rendered by our own 401 envelope.
_bearer = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


async def get_current_user(
    token: Annotated[str | None, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Authenticate the request from its `Authorization: Bearer` header."""
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


CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]
PageParams = Annotated[Pagination, Depends(get_pagination)]
