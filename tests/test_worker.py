import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from fakeredis import FakeAsyncRedis, FakeServer
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.llm import ChatMessage, ChatResult, ToolDefinition
from app.models import AgentRun, AgentSession, RunStatus, RunStep, User
from app.runs import cancellation
from app.runs.runner import RunInProgress
from app.worker import tasks
from app.worker.celery_app import celery_app


def test_enqueue_uses_the_run_id_as_the_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        tasks.execute_agent_run, "apply_async", lambda **kwargs: sent.append(kwargs)
    )

    tasks.enqueue_run("run-123")

    assert sent == [{"args": ["run-123"], "task_id": "run-123"}]


def _raising(exc: Exception) -> Callable[[str], Coroutine[Any, Any, None]]:
    async def execute(run_id: str) -> None:
        raise exc

    return execute


def test_a_run_leased_elsewhere_is_checked_again_after_the_lease_could_expire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tasks, "_execute", _raising(RunInProgress("run-123", retry_in=42)))
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        tasks.execute_agent_run, "apply_async", lambda **kwargs: sent.append(kwargs)
    )

    tasks.execute_agent_run.run("run-123")

    # A fresh message, not a retry: re-checks must not use up the retry budget.
    assert sent == [{"args": ["run-123"], "task_id": "run-123", "countdown": 43}]


def test_revoke_terminates_the_task_with_sigusr1(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        celery_app.control,
        "revoke",
        lambda task_id, **kwargs: sent.append((task_id, kwargs)),
    )

    tasks.revoke_run("run-123")

    assert sent == [("run-123", {"terminate": True, "signal": "SIGUSR1"})]


class _Worker:
    """What the task sees: committed rows (it runs on its own event loops) and a Redis
    whose publishes are recorded."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.session_factory = async_sessionmaker(engine, expire_on_commit=False)
        self.server = FakeServer()
        self.published: list[dict[str, Any]] = []

    def redis(self) -> FakeAsyncRedis:
        return FakeAsyncRedis(server=self.server, decode_responses=True)

    @asynccontextmanager
    async def connections(self) -> AsyncIterator[tuple[Any, FakeAsyncRedis]]:
        redis = self.redis()
        pubsub = redis.pubsub()
        await pubsub.psubscribe("run:*:events")
        try:
            yield self.session_factory, redis
        finally:
            while (message := await pubsub.get_message(timeout=0)) is not None:
                if message["type"] == "pmessage":
                    self.published.append(json.loads(message["data"]))
            await pubsub.aclose()  # type: ignore[no-untyped-call]
            await redis.aclose()


@pytest.fixture
def worker(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Worker]:
    worker = _Worker(engine)
    monkeypatch.setattr(tasks, "_connections", worker.connections)
    yield worker

    async def clean_up() -> None:
        async with worker.session_factory() as db:
            await db.execute(delete(User).where(User.email.like("worker-%@example.com")))
            await db.commit()

    asyncio.run(clean_up())


async def _create_run(worker: _Worker, status: RunStatus) -> str:
    async with worker.session_factory() as db:
        user = User(email=f"worker-{uuid.uuid4().hex}@example.com", hashed_password="x")
        db.add(user)
        await db.flush()
        agent_session = AgentSession(
            user_id=user.id, name="Worker", system_prompt="", tools_enabled=[]
        )
        db.add(agent_session)
        await db.flush()
        run = AgentRun(session_id=agent_session.id, user_message="Hi", status=status)
        db.add(run)
        await db.commit()
        return run.id


async def _outcome(worker: _Worker, run_id: str) -> tuple[RunStatus | None, list[Any]]:
    async with worker.session_factory() as db:
        status = await db.scalar(select(AgentRun.status).where(AgentRun.id == run_id))
        steps = await db.scalars(
            select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.id)
        )
        return status, [(s.step_type, s.payload) for s in steps]


class _RevokedMidCall:
    """An LLM call the task's revoke interrupts, the way SIGUSR1 does in a real worker."""

    def __init__(self, worker: _Worker, *, cancelled_by_api: bool) -> None:
        self._worker = worker
        self._cancelled_by_api = cancelled_by_api

    async def chat(self, messages: list[ChatMessage], tools: list[ToolDefinition]) -> ChatResult:
        run_id = await self._current_run_id()
        if self._cancelled_by_api:
            async with self._worker.session_factory() as db:
                await cancellation.request_cancel(
                    db,
                    self._worker.redis(),
                    run_id,
                    reason=cancellation.CancelReason.BY_USER,
                    revoke=lambda _: None,
                )
        raise SoftTimeLimitExceeded()

    async def _current_run_id(self) -> str:
        async with self._worker.session_factory() as db:
            run_id = await db.scalar(
                select(AgentRun.id).where(AgentRun.status == RunStatus.RUNNING)
            )
        assert run_id is not None
        return run_id

    async def aclose(self) -> None:
        return None


@pytest.mark.parametrize(
    ("cancelled_by_api", "reason"), [(True, "cancelled by user"), (False, "cancelled")]
)
def test_a_task_interrupted_by_its_revoke_settles_the_run_as_cancelled(
    worker: _Worker,
    monkeypatch: pytest.MonkeyPatch,
    cancelled_by_api: bool,
    reason: str,
) -> None:
    run_id = asyncio.run(_create_run(worker, RunStatus.QUEUED))
    llm = _RevokedMidCall(worker, cancelled_by_api=cancelled_by_api)
    monkeypatch.setattr(tasks, "create_llm_provider", lambda settings: llm)
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        tasks.execute_agent_run, "apply_async", lambda **kwargs: sent.append(kwargs)
    )

    tasks.execute_agent_run.run(run_id)

    status, steps = asyncio.run(_outcome(worker, run_id))
    assert status == RunStatus.CANCELLED
    assert [s for s in steps if s[0] != "memory_retrieval"] == [
        ("cancelled", {"reason": reason})
    ]
    assert worker.published[-1] == {"step_type": "done", "status": "cancelled"}
    assert sent == []
