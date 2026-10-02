import asyncio
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
from app.llm.fake import FakeReply, HashEmbedder, ScriptedLLM, answer, call_tool
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

    assert [s.step_type for s in await _steps(db_session, run.id)] == [
        "memory_retrieval",
        "llm_call",
        "final_answer",
    ]
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
    assert [s.step_type for s in steps] == ["memory_retrieval"] + [
        "llm_call",
        "tool_call",
        "tool_result",
    ] * cap + ["final_answer"]
    assert steps[-1].payload == {
        "content": "Max iterations reached",
        "iterations": cap,
        "max_iterations_hit": True,
    }
    finished = await _run(db_session, run.id)
    assert finished.status == RunStatus.COMPLETED
    assert finished.final_answer == "Max iterations reached"
    assert finished.tokens_used == 15 * cap


@pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED])
async def test_terminal_run_is_a_no_op(
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
    assert len(await _steps(db_session, run.id)) == 3
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
        await run_agent(run.id, RunnerDeps(worker_session_factory, redis, llm, HashEmbedder()))
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


async def _tool_messages(llm: ScriptedLLM, request_index: int) -> list[dict[str, Any]]:
    return [m for m in llm.requests[request_index].messages if m["role"] == "tool"]


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        (
            "teleport",
            {},
            "Unknown tool 'teleport'. Available tools: calculator, web_search",
        ),
        (
            "get_current_datetime",
            {},
            "Tool 'get_current_datetime' is not enabled for this session. "
            "Available tools: calculator, web_search",
        ),
        ("calculator", '{"expression": ', "Invalid JSON arguments for 'calculator':"),
        ("calculator", {"expr": "1 + 1"}, "Invalid arguments for 'calculator':"),
    ],
    ids=["hallucinated-tool", "disabled-tool", "malformed-json", "invalid-args"],
)
async def test_bad_tool_calls_become_error_results_and_the_loop_continues(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
    name: str,
    arguments: dict[str, Any] | str,
    expected: str,
) -> None:
    run = await agent_run_factory(
        await agent_session_factory(authed_user, tools_enabled=["calculator", "web_search"])
    )
    llm = ScriptedLLM([call_tool(name, arguments), answer("Recovered.")])

    await execute_run(run.id, llm)

    steps = await _steps(db_session, run.id)
    assert [s.step_type for s in steps] == [
        "memory_retrieval",
        "llm_call",
        "tool_call",
        "tool_result",
        "llm_call",
        "final_answer",
    ]
    result = steps[3].payload
    assert result["tool_call_id"] == "call_1"
    assert result["name"] == name
    assert result["is_error"] is True
    assert result["result"].startswith(expected)
    [tool_message] = await _tool_messages(llm, 1)
    assert tool_message["tool_call_id"] == "call_1"
    assert tool_message["content"].startswith(expected)
    finished = await _run(db_session, run.id)
    assert finished.status == RunStatus.COMPLETED
    assert finished.final_answer == "Recovered."


async def test_disabled_tool_is_refused_even_though_its_schema_was_never_sent(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    isolated_registry: None,
) -> None:
    ran = False

    @tool
    def launch_missiles() -> str:
        """Not something this session may do."""
        nonlocal ran
        ran = True
        return "launched"

    run = await agent_run_factory(
        await agent_session_factory(authed_user, tools_enabled=["calculator"])
    )
    llm = ScriptedLLM([call_tool("launch_missiles", {}), answer("ok")])

    await execute_run(run.id, llm)

    assert [t["function"]["name"] for t in llm.requests[0].tools] == ["calculator"]
    assert ran is False
    [tool_message] = await _tool_messages(llm, 1)
    assert tool_message["content"].startswith("Tool 'launch_missiles' is not enabled")


async def test_tool_exception_becomes_an_error_result_and_the_loop_continues(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
    isolated_registry: None,
) -> None:
    @tool
    def explode() -> str:
        """Always fails."""
        raise RuntimeError("kaboom")

    run = await agent_run_factory(
        await agent_session_factory(authed_user, tools_enabled=["explode"])
    )
    llm = ScriptedLLM([call_tool("explode", {}), answer("It broke.")])

    await execute_run(run.id, llm)

    result = next(s for s in await _steps(db_session, run.id) if s.step_type == "tool_result")
    assert result.payload["is_error"] is True
    assert result.payload["result"] == "Error in 'explode': RuntimeError: kaboom"
    assert (await _run(db_session, run.id)).status == RunStatus.COMPLETED


async def test_tool_timeout_records_a_timeout_step_and_the_loop_continues(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
    isolated_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "tool_timeout_seconds", 0.05)

    @tool
    async def dawdle() -> str:
        """Takes far too long."""
        await asyncio.sleep(10)
        return "finally"

    run = await agent_run_factory(
        await agent_session_factory(authed_user, tools_enabled=["dawdle"])
    )
    llm = ScriptedLLM([call_tool("dawdle", {}), answer("Gave up waiting.")])

    await execute_run(run.id, llm)

    steps = await _steps(db_session, run.id)
    assert [s.step_type for s in steps] == [
        "memory_retrieval",
        "llm_call",
        "tool_call",
        "tool_timeout",
        "llm_call",
        "final_answer",
    ]
    assert steps[3].payload == {
        "tool_call_id": "call_1",
        "name": "dawdle",
        "timeout_seconds": 0.05,
    }
    [tool_message] = await _tool_messages(llm, 1)
    assert tool_message == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "Tool 'dawdle' timed out after 0.05 seconds",
    }
    assert (await _run(db_session, run.id)).status == RunStatus.COMPLETED


async def test_parallel_tool_calls_run_concurrently_and_keep_their_order(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
    isolated_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Run one at a time, neither tool could get past the barrier and both would time out.
    monkeypatch.setattr(get_settings(), "tool_timeout_seconds", 1.0)
    barrier = asyncio.Barrier(2)
    finished_order: list[str] = []

    @tool
    async def slow(text: str) -> str:
        """Wait for the other tool, then take a while longer."""
        await barrier.wait()
        await asyncio.sleep(0.05)
        finished_order.append("slow")
        return f"slow:{text}"

    @tool
    async def fast(text: str) -> str:
        """Wait for the other tool, then return at once."""
        await barrier.wait()
        finished_order.append("fast")
        return f"fast:{text}"

    run = await agent_run_factory(
        await agent_session_factory(authed_user, tools_enabled=["slow", "fast"])
    )
    llm = ScriptedLLM(
        [
            FakeReply(
                tool_calls=[("slow", {"text": "a"}), ("fast", {"text": "b"}), ("teleport", {})]
            ),
            answer("done"),
        ]
    )

    await execute_run(run.id, llm)

    assert finished_order == ["fast", "slow"]
    steps = await _steps(db_session, run.id)
    assert [s.step_type for s in steps] == [
        "memory_retrieval",
        "llm_call",
        "tool_call",
        "tool_call",
        "tool_call",
        "tool_result",
        "tool_result",
        "tool_result",
        "llm_call",
        "final_answer",
    ]
    assert [s.payload["tool_call_id"] for s in steps[2:8]] == ["call_1", "call_2", "call_3"] * 2
    assert [s.payload["is_error"] for s in steps[5:8]] == [False, False, True]
    assert await _tool_messages(llm, 1) == [
        {"role": "tool", "tool_call_id": "call_1", "content": "slow:a"},
        {"role": "tool", "tool_call_id": "call_2", "content": "fast:b"},
        {
            "role": "tool",
            "tool_call_id": "call_3",
            "content": "Unknown tool 'teleport'. Available tools: fast, slow",
        },
    ]


async def test_unhandled_exception_fails_the_run_with_an_error_step(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
    redis: FakeAsyncRedis,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    pubsub = redis.pubsub()
    await pubsub.subscribe(run_channel(run.id))
    # The script runs out on the second call, so the provider raises mid-loop.
    llm = ScriptedLLM([call_tool("calculator", {"expression": "1 + 1"})])

    await execute_run(run.id, llm)

    steps = await _steps(db_session, run.id)
    assert [s.step_type for s in steps] == [
        "memory_retrieval",
        "llm_call",
        "tool_call",
        "tool_result",
        "error",
    ]
    assert steps[-1].payload == {
        "type": "RuntimeError",
        "message": "ScriptedLLM script exhausted after 1 calls",
    }
    finished = await _run(db_session, run.id)
    assert finished.status == RunStatus.FAILED
    assert finished.finished_at is not None
    assert finished.final_answer is None
    assert finished.tokens_used == 15
    events = []
    while (message := await pubsub.get_message(timeout=0)) is not None:
        if message["type"] == "message":
            events.append(json.loads(message["data"]))
    assert events[-1] == {"step_type": "done", "status": "failed"}
    await pubsub.aclose()  # type: ignore[no-untyped-call]


async def test_summarise_text_usage_is_included_in_tokens_used(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    run = await agent_run_factory(
        await agent_session_factory(authed_user, tools_enabled=["summarise_text"])
    )
    llm = ScriptedLLM(
        [
            FakeReply(
                tool_calls=[("summarise_text", {"text": "A long text.", "max_words": 3})],
                usage=LLMUsage(100, 10),
            ),
            # The nested call made by the tool.
            FakeReply(content="One two three four five", usage=LLMUsage(40, 4)),
            FakeReply(content="Summarised.", usage=LLMUsage(200, 3)),
        ]
    )

    await execute_run(run.id, llm)

    nested = llm.requests[1]
    assert nested.tools == []
    assert "3 words" in nested.messages[0]["content"]
    assert nested.messages[-1] == {"role": "user", "content": "A long text."}
    [tool_message] = await _tool_messages(llm, 2)
    assert tool_message["content"] == "One two three"
    finished = await _run(db_session, run.id)
    assert finished.status == RunStatus.COMPLETED
    assert finished.tokens_used == 110 + 44 + 203


class _DoneFailingRedis(FakeAsyncRedis):
    """Publishes steps normally but fails to publish the terminal `done` event."""

    async def publish(self, channel: ChannelT, message: EncodableT, **kwargs: Any) -> int:
        assert isinstance(message, str)
        if json.loads(message)["step_type"] == "done":
            raise ConnectionError("redis went away")
        result: int = await super().publish(channel, message, **kwargs)
        return result


async def test_failure_after_completion_does_not_add_an_error_step(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    worker_session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    redis = _DoneFailingRedis(decode_responses=True)

    try:
        await run_agent(
            run.id, RunnerDeps(
                worker_session_factory, redis, ScriptedLLM([answer("Hi.")]), HashEmbedder()
            )
        )
    finally:
        await redis.aclose()

    assert [s.step_type for s in await _steps(db_session, run.id)] == [
        "memory_retrieval",
        "llm_call",
        "final_answer",
    ]
    assert (await _run(db_session, run.id)).status == RunStatus.COMPLETED
