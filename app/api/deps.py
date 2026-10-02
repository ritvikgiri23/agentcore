from typing import Annotated

import structlog
from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import UnauthorizedError
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models import User

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
