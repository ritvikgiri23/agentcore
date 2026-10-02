from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


async def test_register_returns_access_token(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "ada@example.com", "password": "correct-horse-battery"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 1800
    assert isinstance(body["access_token"], str) and body["access_token"]


async def test_register_duplicate_email_is_conflict(client: AsyncClient) -> None:
    payload = {"email": "ada@example.com", "password": "correct-horse-battery"}
    await client.post("/api/v1/auth/register", json=payload)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "ADA@example.com", "password": "another-password"},
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "conflict"
    assert error["message"] == "Email already registered"


async def test_login_returns_access_token(client: AsyncClient) -> None:
    await client.post(
        "/api/v1/auth/register",
        json={"email": "ada@example.com", "password": "correct-horse-battery"},
    )

    response = await client.post(
        "/api/v1/auth/token",
        data={"username": "ada@example.com", "password": "correct-horse-battery"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 1800
    assert body["access_token"]


async def test_login_wrong_password_is_unauthorized(client: AsyncClient) -> None:
    await client.post(
        "/api/v1/auth/register",
        json={"email": "ada@example.com", "password": "correct-horse-battery"},
    )

    response = await client.post(
        "/api/v1/auth/token",
        data={"username": "ada@example.com", "password": "wrong-password"},
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
    await client.post(
        "/api/v1/auth/register",
        json={"email": "ada@example.com", "password": "correct-horse-battery"},
    )

    stored = await db_session.scalar(
        select(User.hashed_password).where(User.email == "ada@example.com")
    )
    assert stored is not None
    assert "correct-horse-battery" not in stored
