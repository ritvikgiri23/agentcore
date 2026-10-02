from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ErrorResponse, UnauthorizedError
from app.core.security import create_access_token, hash_password_async, verify_password
from app.db.session import get_db
from app.models import User
from app.schemas.auth import PASSWORD_MAX_LENGTH, RegisterRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])


def _email_taken() -> ConflictError:
    return ConflictError("Email already registered", details={"field": "email"})


def _token_response(user: User) -> TokenResponse:
    token = create_access_token(user.id)
    return TokenResponse(access_token=token.token, expires_in=token.expires_in)


@router.post(
    "/register",
    response_model=TokenResponse,
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
    if await db.scalar(select(User.id).where(User.email == email)) is not None:
        raise _email_taken()

    user = User(email=email, hashed_password=await hash_password_async(body.password))
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # Lost a race with a concurrent registration for the same email.
        await db.rollback()
        raise _email_taken() from None
    return _token_response(user)


@router.post(
    "/token",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
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
    # No registered password can be this long; reject before doing any hashing work.
    if len(form.password) > PASSWORD_MAX_LENGTH:
        raise UnauthorizedError("Incorrect email or password")
    user = await db.scalar(select(User).where(User.email == form.username.lower()))
    password_ok = await verify_password(form.password, user.hashed_password if user else None)
    if user is None or not password_ok:
        raise UnauthorizedError("Incorrect email or password")
    return _token_response(user)
