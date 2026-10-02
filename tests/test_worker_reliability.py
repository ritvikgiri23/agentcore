import asyncio
import json
from typing import Any

# The OpenAI SDK's own HTTP client (a dependency of `openai`); its errors are built on it.
import httpx2
import openai
import pytest
from fakeredis import FakeAsyncRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.llm import ChatMessage, ChatResult, ToolDefinition
from app.llm.fake import HashEmbedder, ScriptedLLM, answer, call_tool
from app.llm.retry import RetryingLLM
from app.models import AgentRun, RunStatus, RunStep
from app.runs.lease import RunLease, lease_key
from app.runs.events import run_channel
from app.runs.runner import RunInProgress, RunnerDeps, run_agent
from app.tools import ToolContext, tool
from tests.conftest import (
    AgentRunFactory,
    AgentSessionFactory,
    AuthedUser,
    RunExecutor,
)


async def _steps(db: AsyncSession, run_id: str) -> list[RunStep]:
    result = await db.scalars(
        select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.id)
    )
    return list(result)


async def _run(db: AsyncSession, run_id: str) -> AgentRun:
    run = await db.get(AgentRun, run_id, populate_existing=True)
    assert run is not None
    return run


async def _lease_remaining(redis: FakeAsyncRedis, run_id: str) -> float:
    remaining_ms: int = await redis.pttl(lease_key(run_id))
    return max(remaining_ms, 0) / 1000


async def _published(pubsub: Any) -> list[dict[str, Any]]:
    events = []
    while (message := await pubsub.get_message(timeout=0)) is not None:
        if message["type"] == "message":
            events.append(json.loads(message["data"]))
    return events


async def test_a_running_run_whose_lease_expired_is_failed_as_worker_lost(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
    redis: FakeAsyncRedis,
) -> None:
    # Claimed by a worker that died: running, but nobody holds its lease any more.
    run = await agent_run_factory(
        await agent_session_factory(authed_user), status=RunStatus.RUNNING
    )
    pubsub = redis.pubsub()
    await pubsub.subscribe(run_channel(run.id))
    llm = ScriptedLLM([answer("should not run")])

    await execute_run(run.id, llm)

    assert llm.requests == []
    steps = await _steps(db_session, run.id)
    assert [(s.step_type, s.payload) for s in steps] == [
        ("error", {"type": "WorkerLost", "message": "worker lost"}),
    ]
    finished = await _run(db_session, run.id)
    assert finished.status == RunStatus.FAILED
    assert finished.finished_at is not None
    events = await _published(pubsub)
    assert events[-1] == {"step_type": "done", "status": "failed"}
    await pubsub.aclose()  # type: ignore[no-untyped-call]


async def test_a_run_leased_by_a_live_worker_is_left_alone(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
    redis: FakeAsyncRedis,
) -> None:
    run = await agent_run_factory(
        await agent_session_factory(authed_user), status=RunStatus.RUNNING
    )
    # Another worker is on it and still refreshing its lease.
    assert await RunLease(redis, run.id, ttl_seconds=60).acquire()
    llm = ScriptedLLM([answer("should not run")])

    with pytest.raises(RunInProgress) as raised:
        await execute_run(run.id, llm)

    # The task checks back once the lease could have expired.
    assert 0 < raised.value.retry_in <= 60
    assert llm.requests == []
    assert await _steps(db_session, run.id) == []
    assert (await _run(db_session, run.id)).status == RunStatus.RUNNING
    assert await _lease_remaining(redis, run.id) > 0


async def test_concurrent_invocations_on_a_queued_run_execute_it_once(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    first, second = ScriptedLLM([answer("first")]), ScriptedLLM([answer("second")])

    outcomes = await asyncio.gather(
        execute_run(run.id, first), execute_run(run.id, second), return_exceptions=True
    )

    assert sum(isinstance(o, RunInProgress) for o in outcomes) == 1
    assert len(first.requests) + len(second.requests) == 1
    assert [s.step_type for s in await _steps(db_session, run.id)] == [
        "memory_retrieval",
        "llm_call",
        "final_answer",
    ]
    assert (await _run(db_session, run.id)).status == RunStatus.COMPLETED


async def test_the_lease_is_held_while_the_run_executes_and_released_after(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    redis: FakeAsyncRedis,
    isolated_registry: None,
) -> None:
    seen: list[float] = []

    @tool
    async def check_lease(ctx: ToolContext) -> str:
        """Reports whether the run is leased."""
        seen.append(await _lease_remaining(redis, ctx.run_id))
        return "ok"

    run = await agent_run_factory(
        await agent_session_factory(authed_user, tools_enabled=["check_lease"])
    )

    await execute_run(run.id, ScriptedLLM([call_tool("check_lease", {}), answer("done")]))

    assert len(seen) == 1
    assert 0 < seen[0] <= get_settings().run_lease_ttl_seconds
    assert await _lease_remaining(redis, run.id) == 0


_REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")


def _status_error(cls: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    return cls("provider error", response=httpx2.Response(status, request=_REQUEST), body=None)


class FlakyLLM:
    """Raises the given errors on its first calls, then answers like the scripted LLM."""

    def __init__(self, errors: list[Exception], then: ScriptedLLM) -> None:
        self._errors = list(errors)
        self._then = then
        self.calls = 0

    async def chat(self, messages: list[ChatMessage], tools: list[ToolDefinition]) -> ChatResult:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return await self._then.chat(messages, tools)

    async def aclose(self) -> None:
        return None


def _retrying(llm: FlakyLLM) -> RetryingLLM:
    return RetryingLLM(llm, attempts=3, base_delay_seconds=0)


@pytest.mark.parametrize(
    "error",
    [
        _status_error(openai.RateLimitError, 429),
        _status_error(openai.InternalServerError, 503),
        openai.APITimeoutError(request=_REQUEST),
        openai.APIConnectionError(request=_REQUEST),
    ],
    ids=["429", "5xx", "timeout", "connection"],
)
async def test_the_planner_retries_a_transient_provider_error(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
    error: Exception,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    flaky = FlakyLLM([error], then=ScriptedLLM([answer("Recovered.")]))

    await execute_run(run.id, _retrying(flaky))

    assert flaky.calls == 2
    finished = await _run(db_session, run.id)
    assert finished.status == RunStatus.COMPLETED
    assert finished.final_answer == "Recovered."


async def test_the_planner_gives_up_after_three_attempts_and_the_run_fails(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    flaky = FlakyLLM(
        [_status_error(openai.RateLimitError, 429) for _ in range(3)],
        then=ScriptedLLM([answer("too late")]),
    )

    await execute_run(run.id, _retrying(flaky))

    assert flaky.calls == 3
    assert (await _run(db_session, run.id)).status == RunStatus.FAILED
    error_step = (await _steps(db_session, run.id))[-1]
    assert error_step.step_type == "error"
    assert error_step.payload["type"] == "RateLimitError"


async def test_the_planner_does_not_retry_a_request_the_provider_rejected(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    flaky = FlakyLLM(
        [_status_error(openai.BadRequestError, 400)], then=ScriptedLLM([answer("unreachable")])
    )

    await execute_run(run.id, _retrying(flaky))

    assert flaky.calls == 1
    assert (await _run(db_session, run.id)).status == RunStatus.FAILED


async def test_a_database_outage_before_the_claim_is_raised_for_retry(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    redis: FakeAsyncRedis,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    unreachable = async_sessionmaker(
        create_async_engine("postgresql+asyncpg://nobody@127.0.0.1:1/none", poolclass=NullPool)
    )
    llm = ScriptedLLM([answer("should not run")])
    deps = RunnerDeps(
        session_factory=unreachable, redis=redis, llm=llm, embedder=HashEmbedder()
    )

    # Raised for the task to retry with backoff; nothing was claimed, so a retry is safe.
    with pytest.raises(OSError):
        await run_agent(run.id, deps)

    assert llm.requests == []
    # A retry must not find its own lease and mistake the run for one in progress.
    assert await _lease_remaining(redis, run.id) == 0


async def test_a_stalled_worker_whose_run_was_taken_over_stops(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
    redis: FakeAsyncRedis,
    isolated_registry: None,
) -> None:
    @tool
    async def stall(ctx: ToolContext) -> str:
        """Outlives the lease; meanwhile a redelivery takes the run over."""
        await redis.delete(lease_key(ctx.run_id))
        assert await RunLease(redis, ctx.run_id, ttl_seconds=60).acquire()
        return "back"

    run = await agent_run_factory(
        await agent_session_factory(authed_user, tools_enabled=["stall"])
    )
    llm = ScriptedLLM([call_tool("stall", {}), answer("should not be asked")])

    await execute_run(run.id, llm)

    assert len(llm.requests) == 1
    assert [s.step_type for s in await _steps(db_session, run.id)][-1] == "tool_result"
    # The new holder decides the run's fate, and keeps its lease.
    assert (await _run(db_session, run.id)).status == RunStatus.RUNNING
    assert await _lease_remaining(redis, run.id) > 0


async def test_a_lease_that_lapsed_with_no_takeover_is_retaken_and_the_run_finishes(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
    redis: FakeAsyncRedis,
    isolated_registry: None,
) -> None:
    @tool
    async def stall(ctx: ToolContext) -> str:
        """Outlives the lease; nobody else wants the run."""
        await redis.delete(lease_key(ctx.run_id))
        return "back"

    run = await agent_run_factory(
        await agent_session_factory(authed_user, tools_enabled=["stall"])
    )

    await execute_run(run.id, ScriptedLLM([call_tool("stall", {}), answer("Done.")]))

    finished = await _run(db_session, run.id)
    assert finished.status == RunStatus.COMPLETED
    assert finished.final_answer == "Done."
