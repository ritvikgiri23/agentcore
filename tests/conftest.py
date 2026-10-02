import os

# Tests never talk to OpenAI, regardless of the developer's environment.
os.environ["ENVIRONMENT"] = "test"
os.environ["LLM_PROVIDER"] = "fake"
os.environ.pop("OPENAI_API_KEY", None)

import itertools  # noqa: E402
from collections.abc import AsyncIterator, Awaitable, Callable  # noqa: E402
from dataclasses import dataclass  # noqa: E402

import pytest  # noqa: E402
from fakeredis import FakeAsyncRedis  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.redis import get_redis  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import User  # noqa: E402  (importing app.models registers every table)


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    """Engine on the dedicated test database; tables are created once per test session."""
    engine = create_async_engine(get_settings().test_database_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A connection inside an outer transaction that is rolled back after each test."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            yield conn
        finally:
            await trans.rollback()


@pytest.fixture
async def db_session(db_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """Session whose commits become savepoints, so the outer rollback still undoes them."""
    async with AsyncSession(
        bind=db_connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    ) as session:
        yield session


@pytest.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


@pytest.fixture
def app(db_session: AsyncSession, redis: FakeAsyncRedis) -> FastAPI:
    app = create_app()

    async def _get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def _get_redis() -> AsyncIterator[FakeAsyncRedis]:
        yield redis

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_redis] = _get_redis
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    # Unhandled exceptions are rendered by the app's 500 handler; don't re-raise them here.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@dataclass(frozen=True)
class AuthedUser:
    user: User
    token: str
    password: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


UserFactory = Callable[..., Awaitable[AuthedUser]]


@pytest.fixture
def user_factory(db_session: AsyncSession) -> UserFactory:
    """Create persisted users, each with a valid access token."""
    counter = itertools.count(1)

    async def make(email: str | None = None, password: str = "password-123") -> AuthedUser:
        user = User(
            email=email or f"user{next(counter)}@example.com",
            hashed_password=hash_password(password),
        )
        db_session.add(user)
        await db_session.commit()
        return AuthedUser(user=user, token=create_access_token(user.id).token, password=password)

    return make


@pytest.fixture
async def authed_user(user_factory: UserFactory) -> AuthedUser:
    return await user_factory()
