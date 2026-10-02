from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from tests.conftest import FACTORY_PASSWORD, UserFactory

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery"


async def _register(client: AsyncClient) -> Response:
    return await client.post("/api/v1/auth/register", json={"email": EMAIL, "password": PASSWORD})


async def test_register_returns_access_token(client: AsyncClient) -> None:
    response = await _register(client)

    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 1800
    assert isinstance(body["access_token"], str) and body["access_token"]


async def test_register_duplicate_email_is_conflict(client: AsyncClient) -> None:
    await _register(client)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "ADA@example.com", "password": "another-password"},
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "conflict"
    assert error["message"] == "Email already registered"


async def test_login_returns_access_token(client: AsyncClient) -> None:
    await _register(client)

    response = await client.post(
        "/api/v1/auth/token",
        data={"username": EMAIL, "password": PASSWORD},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 1800
    assert body["access_token"]


async def test_login_wrong_password_is_unauthorized(client: AsyncClient) -> None:
    await _register(client)

    response = await client.post(
        "/api/v1/auth/token",
        data={"username": EMAIL, "password": "wrong-password"},
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    error = response.json()["error"]
    assert error["code"] == "unauthorized"
    assert error["message"] == "Incorrect email or password"


async def test_login_unknown_email_is_unauthorized(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/token",
        data={"username": "nobody@example.com", "password": "whatever-123"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Incorrect email or password"


async def test_register_rejects_invalid_email_and_short_password(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/register", json={"email": "not-an-email", "password": "short"}
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert sorted(d["loc"][-1] for d in error["details"]) == ["email", "password"]


async def test_password_is_not_stored_in_plain_text(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _register(client)

    stored = await db_session.scalar(
        select(User.hashed_password).where(User.email == EMAIL)
    )
    assert stored is not None
    assert PASSWORD not in stored


async def test_factory_user_can_log_in_with_mixed_case_email(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    await user_factory(email="Grace@Example.com")

    response = await client.post(
        "/api/v1/auth/token",
        data={"username": "GRACE@example.com", "password": FACTORY_PASSWORD},
    )

    assert response.status_code == 200


async def test_login_with_overlong_password_is_unauthorized(client: AsyncClient) -> None:
    await _register(client)

    response = await client.post(
        "/api/v1/auth/token", data={"username": EMAIL, "password": "x" * 10_000}
    )

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Incorrect email or password"
