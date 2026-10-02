from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm.fake import ScriptedLLM, answer, call_tool
from app.memory import short_term
from app.models import AgentSession, RunStep
from tests.conftest import (
    AgentRunFactory,
    AgentSessionFactory,
    AuthedUser,
    RunExecutor,
)

PERSONA = "You are a careful research assistant."


async def _retrieval(db: AsyncSession, run_id: str) -> dict[str, Any]:
    first = await db.scalar(
        select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.id).limit(1)
    )
    assert first is not None
    assert first.step_type == "memory_retrieval"
    return first.payload


async def test_the_next_run_sees_the_previous_turn(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
) -> None:
    session = await agent_session_factory(authed_user, system_prompt=PERSONA)
    first = await agent_run_factory(session, "What is the population of Dubai?")
    await execute_run(first.id, ScriptedLLM([answer("About 3.6 million.")]))
    second = await agent_run_factory(session, "And 15% of that?")
    llm = ScriptedLLM([answer("About 540,000.")])

    await execute_run(second.id, llm)

    assert llm.requests[0].messages == [
        {"role": "system", "content": PERSONA},
        {"role": "user", "content": "What is the population of Dubai?"},
        {"role": "assistant", "content": "About 3.6 million."},
        {"role": "user", "content": "And 15% of that?"},
    ]


async def _converse(
    session: AgentSession,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    count: int,
) -> None:
    """Complete `count` runs in the session: question n is answered with answer n."""
    for n in range(1, count + 1):
        run = await agent_run_factory(session, f"question {n}")
        await execute_run(run.id, ScriptedLLM([answer(f"answer {n}")]))


def _turns_seen(llm: ScriptedLLM) -> list[str]:
    """The previous turns' questions in the first request, minus the new message."""
    history = [m for m in llm.requests[0].messages if m["role"] != "system"][:-1]
    return [m["content"] for m in history if m["role"] == "user"]


async def test_only_the_last_five_turns_are_used_as_context(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    session = await agent_session_factory(authed_user)
    await _converse(session, agent_run_factory, execute_run, 7)
    run = await agent_run_factory(session, "question 8")
    llm = ScriptedLLM([answer("answer 8")])

    await execute_run(run.id, llm)

    assert get_settings().short_term_context_messages == 10
    assert _turns_seen(llm) == [f"question {n}" for n in range(3, 8)]
    history = llm.requests[0].messages[1:-1]
    assert history[-2:] == [
        {"role": "user", "content": "question 7"},
        {"role": "assistant", "content": "answer 7"},
    ]
    assert (await _retrieval(db_session, run.id))["short_term_turns"] == 5


async def test_only_the_last_twenty_turns_are_retained(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Widen the window past the retention limit so every retained turn is visible.
    monkeypatch.setattr(get_settings(), "short_term_context_messages", 100)
    session = await agent_session_factory(authed_user)
    await _converse(session, agent_run_factory, execute_run, 21)
    run = await agent_run_factory(session, "question 22")
    llm = ScriptedLLM([answer("answer 22")])

    await execute_run(run.id, llm)

    assert get_settings().short_term_retained_turns == 20
    assert _turns_seen(llm) == [f"question {n}" for n in range(2, 22)]


async def test_a_first_run_has_no_short_term_turns(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user), "Hello?")

    await execute_run(run.id, ScriptedLLM([answer("Hi.")]))

    assert (await _retrieval(db_session, run.id))["short_term_turns"] == 0


async def test_a_failed_run_is_not_remembered(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
) -> None:
    session = await agent_session_factory(authed_user)
    failed = await agent_run_factory(session, "This will fail")
    # An empty script makes the provider raise on the first call.
    await execute_run(failed.id, ScriptedLLM())
    run = await agent_run_factory(session, "Hello?")
    llm = ScriptedLLM([answer("Hi.")])

    await execute_run(run.id, llm)

    assert _turns_seen(llm) == []


async def test_a_run_that_hits_the_iteration_cap_is_not_remembered(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
) -> None:
    session = await agent_session_factory(authed_user)
    capped = await agent_run_factory(session, "Loop forever")
    await execute_run(
        capped.id, ScriptedLLM(fallback=call_tool("calculator", {"expression": "1 + 1"}))
    )
    run = await agent_run_factory(session, "Hello?")
    llm = ScriptedLLM([answer("Hi.")])

    await execute_run(run.id, llm)

    assert _turns_seen(llm) == []


async def test_an_empty_answer_is_not_remembered(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
) -> None:
    session = await agent_session_factory(authed_user)
    empty = await agent_run_factory(session, "Say nothing")
    await execute_run(empty.id, ScriptedLLM([answer("")]))
    run = await agent_run_factory(session, "Hello?")
    llm = ScriptedLLM([answer("Hi.")])

    await execute_run(run.id, llm)

    assert _turns_seen(llm) == []


async def test_turns_stay_within_their_session(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
) -> None:
    await _converse(await agent_session_factory(authed_user), agent_run_factory, execute_run, 1)
    run = await agent_run_factory(await agent_session_factory(authed_user), "Hello?")
    llm = ScriptedLLM([answer("Hi.")])

    await execute_run(run.id, llm)

    assert _turns_seen(llm) == []


async def test_deleting_a_session_clears_its_history(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    redis: FakeAsyncRedis,
) -> None:
    session = await agent_session_factory(authed_user)
    await _converse(session, agent_run_factory, execute_run, 2)
    assert await redis.exists(short_term.history_key(session.id))

    deleted = await client.delete(f"/api/v1/sessions/{session.id}", headers=authed_user.headers)

    assert deleted.status_code == 204
    assert not await redis.exists(short_term.history_key(session.id))
