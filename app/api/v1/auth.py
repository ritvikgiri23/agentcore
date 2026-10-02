from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ErrorResponse, UnauthorizedError
from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.models import User
from app.schemas.auth import RegisterRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_response(user: User) -> TokenResponse:
    token = create_access_token(user.id)
    return TokenResponse(access_token=token.token, expires_in=token.expires_in)


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user and receive an access token",
    responses={
        409: {"model": ErrorResponse, "description": "Email already registered"},
        422: {"model": ErrorResponse, "description": "Invalid email or password"},
    },
)
async def register(
    body: RegisterRequest, db: Annotated[AsyncSession, Depends(get_db)]
) -> TokenResponse:
    email = body.email.lower()
    conflict = ConflictError("Email already registered", details={"field": "email"})
    if await db.scalar(select(User.id).where(User.email == email)) is not None:
        raise conflict

    user = User(email=email, hashed_password=hash_password(body.password))
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # Lost a race with a concurrent registration for the same email.
        await db.rollback()
        raise conflict from None
    return _token_response(user)


@router.post(
    "/token",
    summary="Log in with email and password (OAuth2 password form)",
    responses={
        401: {"model": ErrorResponse, "description": "Incorrect email or password"},
        422: {"model": ErrorResponse, "description": "Missing form fields"},
    },
)
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    user = await db.scalar(select(User).where(User.email == form.username.lower()))
    if not verify_password(form.password, user.hashed_password if user else None):
        raise UnauthorizedError("Incorrect email or password")
    assert user is not None
    return _token_response(user)
