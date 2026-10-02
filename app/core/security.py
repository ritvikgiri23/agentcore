import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jose import ExpiredSignatureError, JWTError, jwt
from passlib.context import CryptContext

from app.core.config import get_settings
from app.core.errors import UnauthorizedError

_pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

# Verified against when the email is unknown, so login timing doesn't reveal which emails exist.
_DUMMY_HASH = _pwd_context.hash(uuid.uuid4().hex)


# Hashing is deliberately slow (tens of ms); run it off the event loop.


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


async def hash_password_async(password: str) -> str:
    return await asyncio.to_thread(hash_password, password)


async def verify_password(password: str, hashed_password: str | None) -> bool:
    """Check a password; with no stored hash, burn equivalent time and return False."""
    target = hashed_password if hashed_password is not None else _DUMMY_HASH
    matches = await asyncio.to_thread(_pwd_context.verify, password, target)
    return matches and hashed_password is not None


@dataclass(frozen=True)
class AccessToken:
    token: str
    expires_in: int


def create_access_token(user_id: str) -> AccessToken:
    settings = get_settings()
    now = datetime.now(UTC)
    lifetime = timedelta(minutes=settings.jwt_expire_minutes)
    claims = {"sub": user_id, "type": "access", "iat": now, "exp": now + lifetime}
    token = jwt.encode(
        claims, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    return AccessToken(token=token, expires_in=int(lifetime.total_seconds()))


def decode_access_token(token: str) -> str:
    """Return the user id in a valid access token, else raise UnauthorizedError."""
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require_exp": True, "require_sub": True},
        )
    except ExpiredSignatureError:
        raise UnauthorizedError("Access token has expired") from None
    except JWTError:
        raise UnauthorizedError("Invalid access token") from None

    subject = claims.get("sub")
    if claims.get("type") != "access" or not isinstance(subject, str):
        raise UnauthorizedError("Invalid access token")
    return subject
