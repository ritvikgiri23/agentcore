import os

# Tests never talk to OpenAI, regardless of the developer's environment.
os.environ["ENVIRONMENT"] = "test"
os.environ["LLM_PROVIDER"] = "fake"
os.environ.pop("OPENAI_API_KEY", None)

import itertools  # noqa: E402
from collections.abc import AsyncIterator, Awaitable, Iterator  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from typing import Protocol  # noqa: E402

import pytest  # noqa: E402
from fakeredis import FakeAsyncRedis  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

from app.api.deps import get_run_enqueuer  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.redis import get_redis  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import create_app  # noqa: E402
# Importing app.models registers every table.
from app.llm import LLMProvider  # noqa: E402
from app.models import AgentRun, AgentSession, RunStatus, RunStep, User  # noqa: E402
from app.runs.runner import RunnerDeps, run_agent  # noqa: E402
from app.tools import TOOL_REGISTRY, ToolContext  # noqa: E402


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
def enqueued() -> list[str]:
    """Run ids the API handed to the broker; tests never talk to a real one."""
    return []


@pytest.fixture
def app(db_session: AsyncSession, redis: FakeAsyncRedis, enqueued: list[str]) -> FastAPI:
    app = create_app()

    async def _get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def _get_redis() -> AsyncIterator[FakeAsyncRedis]:
        yield redis

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_redis] = _get_redis
    app.dependency_overrides[get_run_enqueuer] = lambda: enqueued.append
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    # Unhandled exceptions are rendered by the app's 500 handler; don't re-raise them here.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def isolated_registry() -> Iterator[None]:
    """Let a test register its own tools without leaking them into other tests."""
    saved = dict(TOOL_REGISTRY)
    try:
        yield
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(saved)


@pytest.fixture
def tool_context() -> ToolContext:
    return ToolContext(user_id="test-user", run_id="test-run")


@dataclass(frozen=True)
class AuthedUser:
    user: User
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


class UserFactory(Protocol):
    def __call__(self, email: str | None = None) -> Awaitable[AuthedUser]: ...


FACTORY_PASSWORD = "password-123"
_FACTORY_PASSWORD_HASH = hash_password(FACTORY_PASSWORD)


@pytest.fixture
def user_factory(db_session: AsyncSession) -> UserFactory:
    """Create persisted users, each with a valid access token."""
    counter = itertools.count(1)

    async def make(email: str | None = None) -> AuthedUser:
        user = User(
            # Stored lowercased, matching how register and login normalise emails.
            email=(email or f"user{next(counter)}@example.com").lower(),
            hashed_password=_FACTORY_PASSWORD_HASH,
        )
        db_session.add(user)
        await db_session.commit()
        return AuthedUser(user=user, token=create_access_token(user.id).token)

    return make


@pytest.fixture
async def authed_user(user_factory: UserFactory) -> AuthedUser:
    return await user_factory()


class AgentSessionFactory(Protocol):
    def __call__(
        self,
        owner: AuthedUser,
        name: str = ...,
        system_prompt: str = ...,
        tools_enabled: list[str] | None = None,
    ) -> Awaitable[AgentSession]: ...


@pytest.fixture
def agent_session_factory(db_session: AsyncSession) -> AgentSessionFactory:
    """Create persisted agent sessions owned by the given user."""

    async def make(
        owner: AuthedUser,
        name: str = "Research Assistant",
        system_prompt: str = "You are a careful research assistant.",
        tools_enabled: list[str] | None = None,
    ) -> AgentSession:
        if tools_enabled is None:
            tools_enabled = ["web_search", "calculator"]
        agent_session = AgentSession(
            user_id=owner.user.id,
            name=name,
            system_prompt=system_prompt,
            tools_enabled=tools_enabled,
        )
        db_session.add(agent_session)
        await db_session.commit()
        return agent_session

    return make


class AgentRunFactory(Protocol):
    def __call__(
        self,
        agent_session: AgentSession,
        user_message: str = ...,
        status: RunStatus = ...,
    ) -> Awaitable[AgentRun]: ...


@pytest.fixture
def agent_run_factory(db_session: AsyncSession) -> AgentRunFactory:
    """Create persisted runs in a given state."""

    async def make(
        agent_session: AgentSession,
        user_message: str = "What is 15% of 3,655,000?",
        status: RunStatus = RunStatus.QUEUED,
    ) -> AgentRun:
        run = AgentRun(session_id=agent_session.id, user_message=user_message, status=status)
        db_session.add(run)
        await db_session.commit()
        return run

    return make


@pytest.fixture
def worker_session_factory(db_connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """What the worker's per-task engine provides, bound to the test transaction."""
    return async_sessionmaker(
        bind=db_connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


class RunExecutor(Protocol):
    def __call__(self, run_id: str, llm: LLMProvider) -> Awaitable[None]: ...


@pytest.fixture
def execute_run(
    db_session: AsyncSession,
    redis: FakeAsyncRedis,
    worker_session_factory: async_sessionmaker[AsyncSession],
) -> RunExecutor:
    """Execute a run in-process through the entry point the Celery task uses."""

    async def execute(run_id: str, llm: LLMProvider) -> None:
        await run_agent(
            run_id, RunnerDeps(session_factory=worker_session_factory, redis=redis, llm=llm)
        )
        # The worker wrote through its own sessions, so cached runs and steps are stale.
        # Expunged rather than expired: tests keep reading attributes of objects they hold.
        for obj in list(db_session.identity_map.values()):
            if isinstance(obj, (AgentRun, RunStep)):
                db_session.expunge(obj)

    return execute
