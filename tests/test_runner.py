import json
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from redis.typing import ChannelT, EncodableT
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.llm import LLMUsage
from app.llm.fake import FakeReply, ScriptedLLM, answer, call_tool
from app.models import AgentRun, RunStatus, RunStep
from app.runs.events import run_channel
from app.runs.runner import RunnerDeps, run_agent
from app.tools import tool
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


async def test_plain_answer_stops_after_one_llm_call(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    agent_session = await agent_session_factory(
        authed_user, system_prompt="You are terse.", tools_enabled=["calculator"]
    )
    run = await agent_run_factory(agent_session, "Hello?")
    llm = ScriptedLLM([answer("Hi.")])

    await execute_run(run.id, llm)

    assert [s.step_type for s in await _steps(db_session, run.id)] == ["llm_call", "final_answer"]
    finished = await _run(db_session, run.id)
    assert finished.status == RunStatus.COMPLETED
    assert finished.final_answer == "Hi."
    assert finished.tokens_used == 15
    assert len(llm.requests) == 1
    request = llm.requests[0]
    assert request.messages == [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "Hello?"},
    ]
    assert [t["function"]["name"] for t in request.tools] == ["calculator"]


async def test_tool_results_are_fed_back_to_the_llm(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    llm = ScriptedLLM([call_tool("calculator", {"expression": "2 + 2"}), answer("4")])

    await execute_run(run.id, llm)

    second = llm.requests[1].messages
    assert second[2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "calculator", "arguments": '{"expression": "2 + 2"}'},
            }
        ],
    }
    assert second[3] == {"role": "tool", "tool_call_id": "call_1", "content": "4"}


async def test_stops_at_the_iteration_cap(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    llm = ScriptedLLM(fallback=call_tool("calculator", {"expression": "1 + 1"}))

    await execute_run(run.id, llm)

    cap = get_settings().max_iterations
    assert cap == 10
    assert len(llm.requests) == cap
    steps = await _steps(db_session, run.id)
    assert [s.step_type for s in steps] == ["llm_call", "tool_call", "tool_result"] * cap + [
        "final_answer"
    ]
    assert steps[-1].payload == {
        "content": "Max iterations reached",
        "iterations": cap,
        "max_iterations_hit": True,
    }
    finished = await _run(db_session, run.id)
    assert finished.status == RunStatus.COMPLETED
    assert finished.final_answer == "Max iterations reached"
    assert finished.tokens_used == 15 * cap


@pytest.mark.parametrize(
    "status", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.RUNNING]
)
async def test_run_not_in_queued_state_is_a_no_op(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
    redis: FakeAsyncRedis,
    status: RunStatus,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user), status=status)
    pubsub = redis.pubsub()
    await pubsub.subscribe(run_channel(run.id))
    llm = ScriptedLLM([answer("should not run")])

    await execute_run(run.id, llm)

    assert llm.requests == []
    assert await _steps(db_session, run.id) == []
    assert (await _run(db_session, run.id)).status == status
    await pubsub.get_message(timeout=0)  # the subscribe confirmation
    assert await pubsub.get_message(timeout=0) is None
    await pubsub.aclose()  # type: ignore[no-untyped-call]


async def test_second_invocation_on_completed_run_is_a_no_op(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    await execute_run(run.id, ScriptedLLM([answer("first")]))
    first_finish = (await _run(db_session, run.id)).finished_at

    second_llm = ScriptedLLM([answer("second")])
    await execute_run(run.id, second_llm)

    assert second_llm.requests == []
    assert len(await _steps(db_session, run.id)) == 2
    finished = await _run(db_session, run.id)
    assert finished.final_answer == "first"
    assert finished.finished_at == first_finish


async def test_unknown_run_is_a_no_op(execute_run: RunExecutor) -> None:
    llm = ScriptedLLM()

    await execute_run("00000000-0000-4000-8000-000000000000", llm)

    assert llm.requests == []


async def test_long_tool_results_are_truncated_in_the_trace_only(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
    isolated_registry: None,
) -> None:
    @tool
    def big_output() -> str:
        """Return a lot of text."""
        return "x" * 10_000

    run = await agent_run_factory(
        await agent_session_factory(authed_user, tools_enabled=["big_output"])
    )
    llm = ScriptedLLM([call_tool("big_output", {}), answer("done")])

    await execute_run(run.id, llm)

    result_step = next(
        s for s in await _steps(db_session, run.id) if s.step_type == "tool_result"
    )
    limit = get_settings().tool_result_max_chars
    assert len(result_step.payload["result"]) == limit
    assert result_step.payload["truncated"] is True
    # The model still gets the whole result.
    assert llm.requests[1].messages[-1]["content"] == "x" * 10_000


class _PersistenceCheckingRedis(FakeAsyncRedis):
    """Records each published event and whether its step was already in the database."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(decode_responses=True)
        self._session_factory = session_factory
        self.published: list[tuple[dict[str, Any], bool]] = []

    async def publish(self, channel: ChannelT, message: EncodableT, **kwargs: Any) -> int:
        assert isinstance(message, str)
        event = json.loads(message)
        persisted = False
        if "id" in event:
            async with self._session_factory() as db:
                persisted = await db.get(RunStep, event["id"]) is not None
        self.published.append((event, persisted))
        result: int = await super().publish(channel, message, **kwargs)
        return result


async def test_every_published_event_is_already_persisted(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    worker_session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    redis = _PersistenceCheckingRedis(worker_session_factory)
    pubsub = redis.pubsub()
    await pubsub.subscribe(run_channel(run.id))
    llm = ScriptedLLM([call_tool("calculator", {"expression": "6 * 7"}), answer("42")])

    try:
        await run_agent(run.id, RunnerDeps(worker_session_factory, redis, llm))
    finally:
        await redis.aclose()

    *step_events, done = redis.published
    assert done == ({"step_type": "done", "status": "completed"}, False)
    assert all(persisted for _, persisted in step_events)
    steps = await _steps(db_session, run.id)
    assert [event for event, _ in step_events] == [
        {
            "id": s.id,
            "step_type": s.step_type,
            "payload": s.payload,
            "occurred_at": s.occurred_at.isoformat(),
        }
        for s in steps
    ]


async def test_usage_is_added_to_tokens_used_per_llm_call(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    llm = ScriptedLLM(
        [
            FakeReply(tool_calls=[("calculator", {"expression": "1"})], usage=LLMUsage(100, 20)),
            FakeReply(content="1", usage=LLMUsage(150, 7)),
        ]
    )

    await execute_run(run.id, llm)

    assert (await _run(db_session, run.id)).tokens_used == 277


async def test_database_rejects_unknown_step_types(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    db_session: AsyncSession,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    db_session.add(RunStep(run_id=run.id, step_type="done", payload={}))

    with pytest.raises(IntegrityError, match="ck_run_steps_step_type"):
        await db_session.flush()
    await db_session.rollback()
