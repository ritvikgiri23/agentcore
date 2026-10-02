from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from httpx import AsyncClient
from jose import jwt

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.models import User
from tests.conftest import AuthedUser


@pytest.fixture(autouse=True)
def protected_route(app: FastAPI) -> None:
    @app.get("/api/v1/_whoami")
    async def whoami(user: Annotated[User, Depends(get_current_user)]) -> dict[str, str]:
        return {"id": user.id, "email": user.email}


def _token(claims: dict[str, object], secret: str | None = None) -> str:
    settings = get_settings()
    return jwt.encode(
        claims,
        secret or settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


async def test_valid_token_injects_the_user(
    client: AsyncClient, authed_user: AuthedUser
) -> None:
    response = await client.get("/api/v1/_whoami", headers=authed_user.headers)

    assert response.status_code == 200
    assert response.json() == {"id": authed_user.user.id, "email": authed_user.user.email}


async def test_token_from_register_authenticates(client: AsyncClient) -> None:
    registered = await client.post(
        "/api/v1/auth/register",
        json={"email": "grace@example.com", "password": "correct-horse-battery"},
    )
    token = registered.json()["access_token"]

    response = await client.get("/api/v1/_whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["email"] == "grace@example.com"


async def test_missing_token_is_unauthorized(client: AsyncClient) -> None:
    response = await client.get("/api/v1/_whoami")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    error = response.json()["error"]
    assert error["code"] == "unauthorized"
    assert error["message"] == "Not authenticated"
    assert error["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.parametrize(
    "authorization",
    ["Bearer not-a-jwt", "Bearer ", "Basic dXNlcjpwYXNz", "Bearer a.b.c"],
)
async def test_malformed_token_is_unauthorized(client: AsyncClient, authorization: str) -> None:
    response = await client.get("/api/v1/_whoami", headers={"Authorization": authorization})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_expired_token_is_unauthorized(
    client: AsyncClient, authed_user: AuthedUser
) -> None:
    issued = datetime.now(UTC) - timedelta(minutes=31)
    token = _token(
        {"sub": authed_user.user.id, "type": "access", "iat": issued, "exp": issued + timedelta(minutes=30)}
    )

    response = await client.get("/api/v1/_whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Access token has expired"


async def test_token_signed_with_another_secret_is_unauthorized(
    client: AsyncClient, authed_user: AuthedUser
) -> None:
    exp = datetime.now(UTC) + timedelta(minutes=30)
    token = _token({"sub": authed_user.user.id, "type": "access", "exp": exp}, secret="forged")

    response = await client.get("/api/v1/_whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid access token"


async def test_token_for_unknown_user_is_unauthorized(client: AsyncClient) -> None:
    exp = datetime.now(UTC) + timedelta(minutes=30)
    token = _token({"sub": "00000000-0000-0000-0000-000000000000", "type": "access", "exp": exp})

    response = await client.get("/api/v1/_whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
