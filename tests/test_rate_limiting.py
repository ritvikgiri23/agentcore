import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.api.deps import get_run_enqueuer
from app.core.redis import get_redis
from app.core.security import create_access_token
from app.db.session import get_db
from app.llm.fake import ScriptedLLM, answer
from app.models import AgentRun, AgentSession, RunStatus, User
from app.runs import admission, lifecycle
from tests.conftest import AgentSessionFactory, AuthedUser, RunExecutor

SESSIONS = "/api/v1/sessions"
RUNS = "/api/v1/runs"
LIMIT = 10


async def _post_run(client: AsyncClient, user: AuthedUser, session_id: str) -> Response:
    return await client.post(
        f"{SESSIONS}/{session_id}/run", json={"message": "hello"}, headers=user.headers
    )


async def _fill(
    client: AsyncClient, user: AuthedUser, agent_session: AgentSession, count: int = LIMIT
) -> list[str]:
    run_ids = []
    for _ in range(count):
        response = await _post_run(client, user, agent_session.id)
        assert response.status_code == 202, response.text
        run_ids.append(response.json()["run_id"])
    return run_ids


async def _assert_slot_free(
    client: AsyncClient, user: AuthedUser, agent_session: AgentSession
) -> None:
    """The next submission is admitted straight away, without healing drift."""
    with capture_logs() as logs:
        response = await _post_run(client, user, agent_session.id)
    assert response.status_code == 202, response.text
    assert "active_runs_reconciled" not in [log["event"] for log in logs]
    # Exactly one slot was freed.
    _assert_rate_limited(await _post_run(client, user, agent_session.id))


def _assert_rate_limited(response: Response) -> None:
    assert response.status_code == 429, response.text
    error: dict[str, Any] = response.json()["error"]
    assert error["code"] == "rate_limited"
    assert error["details"] == {"limit": LIMIT}


async def test_the_eleventh_active_run_is_rejected(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    db_session: AsyncSession,
    enqueued: list[str],
) -> None:
    agent_session = await agent_session_factory(authed_user)
    await _fill(client, authed_user, agent_session)

    response = await _post_run(client, authed_user, agent_session.id)

    _assert_rate_limited(response)
    assert len(enqueued) == LIMIT
    assert await db_session.scalar(select(func.count()).select_from(AgentRun)) == LIMIT


async def test_a_completed_run_frees_its_slot(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    execute_run: RunExecutor,
) -> None:
    agent_session = await agent_session_factory(authed_user)
    run_ids = await _fill(client, authed_user, agent_session)
    _assert_rate_limited(await _post_run(client, authed_user, agent_session.id))

    await execute_run(run_ids[0], ScriptedLLM([answer("Hi.")]))

    await _assert_slot_free(client, authed_user, agent_session)


async def test_a_failed_run_frees_its_slot(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    execute_run: RunExecutor,
) -> None:
    agent_session = await agent_session_factory(authed_user)
    run_ids = await _fill(client, authed_user, agent_session)

    # An empty script makes the provider raise on the first call.
    await execute_run(run_ids[0], ScriptedLLM([]))

    await _assert_slot_free(client, authed_user, agent_session)


async def test_a_run_whose_worker_was_lost_frees_its_slot(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    agent_session = await agent_session_factory(authed_user)
    run_ids = await _fill(client, authed_user, agent_session)
    # A worker claimed it, then died without its lease being refreshed.
    assert await lifecycle.claim(db_session, run_ids[0]) is not None

    await execute_run(run_ids[0], ScriptedLLM([answer("should not run")]))

    await _assert_slot_free(client, authed_user, agent_session)


async def test_a_cancelled_run_frees_its_slot(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
) -> None:
    agent_session = await agent_session_factory(authed_user)
    run_ids = await _fill(client, authed_user, agent_session)

    response = await client.delete(f"{RUNS}/{run_ids[0]}", headers=authed_user.headers)
    assert response.status_code == 200, response.text

    await _assert_slot_free(client, authed_user, agent_session)


async def test_a_run_that_could_not_be_queued_frees_its_slot(
    app: FastAPI,
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    enqueued: list[str],
) -> None:
    agent_session = await agent_session_factory(authed_user)
    await _fill(client, authed_user, agent_session, count=LIMIT - 1)

    def broken_broker(run_id: str) -> None:
        raise ConnectionError("broker unreachable")

    app.dependency_overrides[get_run_enqueuer] = lambda: broken_broker
    assert (await _post_run(client, authed_user, agent_session.id)).status_code == 503
    app.dependency_overrides[get_run_enqueuer] = lambda: enqueued.append

    await _assert_slot_free(client, authed_user, agent_session)


async def test_a_double_release_is_harmless(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    execute_run: RunExecutor,
    redis: FakeAsyncRedis,
) -> None:
    agent_session = await agent_session_factory(authed_user)
    run_ids = await _fill(client, authed_user, agent_session)
    await execute_run(run_ids[0], ScriptedLLM([answer("Hi.")]))
    # Cancelling a finished run is refused, and a duplicate delivery of the finished run
    # is not claimed: neither frees a second slot.
    response = await client.delete(f"{RUNS}/{run_ids[0]}", headers=authed_user.headers)
    assert response.status_code == 409
    await execute_run(run_ids[0], ScriptedLLM([answer("Hi again.")]))
    # A release retried after its reply was lost.
    await admission.release(redis, authed_user.user.id, run_ids[0])

    await _assert_slot_free(client, authed_user, agent_session)


async def test_runs_that_finished_without_freeing_their_slot_are_reconciled(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    db_session: AsyncSession,
) -> None:
    agent_session = await agent_session_factory(authed_user)
    run_ids = await _fill(client, authed_user, agent_session)
    # Finished while their releases were lost, e.g. to a crash or a Redis outage.
    await db_session.execute(
        update(AgentRun).where(AgentRun.id.in_(run_ids[:3])).values(status=RunStatus.FAILED)
    )
    await db_session.commit()

    with capture_logs() as logs:
        response = await _post_run(client, authed_user, agent_session.id)

    assert response.status_code == 202, response.text
    assert "active_runs_reconciled" in [log["event"] for log in logs]
    # Only the stale slots were freed; the live runs still hold theirs.
    await _fill(client, authed_user, agent_session, count=2)
    _assert_rate_limited(await _post_run(client, authed_user, agent_session.id))


async def test_reconciling_also_counts_active_runs_missing_from_the_set(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    db_session: AsyncSession,
    redis: FakeAsyncRedis,
) -> None:
    agent_session = await agent_session_factory(authed_user)
    run_ids = await _fill(client, authed_user, agent_session)
    # Three finished without freeing their slots; two live ones lost theirs.
    await db_session.execute(
        update(AgentRun).where(AgentRun.id.in_(run_ids[:3])).values(status=RunStatus.FAILED)
    )
    await db_session.commit()
    for run_id in run_ids[3:5]:
        await admission.release(redis, authed_user.user.id, run_id)

    # Seven runs are really active, so three more fit.
    await _fill(client, authed_user, agent_session, count=3)
    _assert_rate_limited(await _post_run(client, authed_user, agent_session.id))


async def test_slots_of_runs_missing_from_the_database_are_reconciled_once_old(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_session = await agent_session_factory(authed_user)
    run_ids = await _fill(client, authed_user, agent_session)
    await db_session.execute(delete(AgentRun).where(AgentRun.id == run_ids[0]))
    await db_session.commit()

    # Just admitted, it may be a run about to be inserted: its slot is kept.
    _assert_rate_limited(await _post_run(client, authed_user, agent_session.id))

    monkeypatch.setattr(admission, "INSERT_GRACE_SECONDS", 0)
    assert (await _post_run(client, authed_user, agent_session.id)).status_code == 202
    _assert_rate_limited(await _post_run(client, authed_user, agent_session.id))


@pytest.fixture
async def committed_user(engine: AsyncEngine) -> AsyncIterator[tuple[AuthedUser, AgentSession]]:
    """A user and session visible to other connections, removed afterwards."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        user = User(email=f"limit-{uuid.uuid4().hex}@example.com", hashed_password="x")
        db.add(user)
        await db.flush()
        agent_session = AgentSession(
            user_id=user.id, name="Limits", system_prompt="", tools_enabled=[]
        )
        db.add(agent_session)
        await db.commit()
    try:
        yield AuthedUser(user=user, token=create_access_token(user.id).token), agent_session
    finally:
        async with session_factory() as db:
            await db.execute(delete(User).where(User.id == user.id))
            await db.commit()


class _NetworkRedis(FakeAsyncRedis):
    """Yields to the event loop on every command, as a round trip to real Redis does.

    Once `contenders` is set, each request's first command waits until that many
    requests have reached theirs, so they race from the same point.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.contenders: asyncio.Barrier | None = None
        self._started: set[asyncio.Task[Any] | None] = set()

    async def execute_command(self, *args: Any, **options: Any) -> Any:
        task = asyncio.current_task()
        if self.contenders is not None and task not in self._started:
            self._started.add(task)
            await self.contenders.wait()
        await asyncio.sleep(0)
        return await super().execute_command(*args, **options)  # type: ignore[no-untyped-call]


async def test_simultaneous_submissions_for_the_last_slot_admit_exactly_one(
    app: FastAPI,
    client: AsyncClient,
    engine: AsyncEngine,
    committed_user: tuple[AuthedUser, AgentSession],
) -> None:
    user, agent_session = committed_user
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _own_session() -> AsyncIterator[AsyncSession]:
        # One connection per request, so the two interleave at every await.
        async with session_factory() as db:
            yield db

    redis = _NetworkRedis(decode_responses=True)

    async def _network_redis() -> AsyncIterator[FakeAsyncRedis]:
        yield redis

    app.dependency_overrides[get_db] = _own_session
    app.dependency_overrides[get_redis] = _network_redis
    await _fill(client, user, agent_session, count=LIMIT - 1)

    redis.contenders = asyncio.Barrier(2)
    responses = await asyncio.gather(
        _post_run(client, user, agent_session.id), _post_run(client, user, agent_session.id)
    )

    assert sorted(r.status_code for r in responses) == [202, 429]
    async with session_factory() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(AgentRun.session_id == agent_session.id)
        )
    assert count == LIMIT
    await redis.aclose()
