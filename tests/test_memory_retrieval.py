from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm.fake import ScriptedLLM, answer, call_tool
from app.models import AgentRun, RunStatus, RunStep
from tests.conftest import (
    AgentRunFactory,
    AgentSessionFactory,
    AuthedUser,
    FailingEmbedder,
    MemoryFactory,
    RunExecutor,
    UserFactory,
)

PERSONA = "You are a careful research assistant."


async def _steps(db: AsyncSession, run_id: str) -> list[RunStep]:
    result = await db.scalars(
        select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.id)
    )
    return list(result)


async def _retrieval(db: AsyncSession, run_id: str) -> dict[str, Any]:
    first = (await _steps(db, run_id))[0]
    assert first.step_type == "memory_retrieval"
    return first.payload


async def test_relevant_memories_are_added_to_the_system_message_and_traced(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    memory_factory: MemoryFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    dubai = await memory_factory(authed_user, "The user lives in Dubai")
    metric = await memory_factory(authed_user, "The user prefers metric units")
    run = await agent_run_factory(
        await agent_session_factory(authed_user, system_prompt=PERSONA),
        "What is the weather in Dubai today?",
    )
    llm = ScriptedLLM([answer("Sunny.")])

    await execute_run(run.id, llm)

    system = llm.requests[0].messages[0]
    assert system["role"] == "system"
    assert system["content"].startswith(PERSONA + "\n\n")
    assert "- The user lives in Dubai" in system["content"]
    assert "may be outdated" in system["content"]
    assert llm.requests[0].messages[1:] == [
        {"role": "user", "content": "What is the weather in Dubai today?"}
    ]
    memories = (await _retrieval(db_session, run.id))["memories"]
    # Nearest first: the metric-units memory shares only "the" with the message.
    assert [(m["id"], m["content"]) for m in memories] == [
        (dubai.id, "The user lives in Dubai"),
        (metric.id, "The user prefers metric units"),
    ]
    assert 0 < memories[0]["distance"] < memories[1]["distance"]


async def test_at_most_three_memories_are_retrieved(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    memory_factory: MemoryFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    for n in range(5):
        await memory_factory(authed_user, f"Fact number {n} about Dubai")
    run = await agent_run_factory(await agent_session_factory(authed_user), "Dubai?")

    await execute_run(run.id, ScriptedLLM([answer("ok")]))

    assert get_settings().long_term_top_k == 3
    assert len((await _retrieval(db_session, run.id))["memories"]) == 3


async def test_memories_beyond_the_distance_cutoff_are_left_out(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    memory_factory: MemoryFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "memory_max_distance", 0.7)
    dubai = await memory_factory(authed_user, "The user lives in Dubai")
    await memory_factory(authed_user, "The user prefers metric units")
    run = await agent_run_factory(
        await agent_session_factory(authed_user), "What is the weather in Dubai today?"
    )
    llm = ScriptedLLM([answer("Sunny.")])

    await execute_run(run.id, llm)

    memories = (await _retrieval(db_session, run.id))["memories"]
    assert [m["id"] for m in memories] == [dubai.id]
    assert "metric" not in llm.requests[0].messages[0]["content"]


async def test_no_relevant_memories_means_no_memory_block(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    run = await agent_run_factory(
        await agent_session_factory(authed_user, system_prompt=PERSONA), "Hello?"
    )
    llm = ScriptedLLM([answer("Hi.")])

    await execute_run(run.id, llm)

    assert llm.requests[0].messages == [
        {"role": "system", "content": PERSONA},
        {"role": "user", "content": "Hello?"},
    ]
    assert (await _retrieval(db_session, run.id))["memories"] == []


async def test_memories_get_a_system_message_even_without_a_persona(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    memory_factory: MemoryFactory,
    execute_run: RunExecutor,
) -> None:
    await memory_factory(authed_user, "The user lives in Dubai")
    run = await agent_run_factory(
        await agent_session_factory(authed_user, system_prompt=""), "Dubai weather?"
    )
    llm = ScriptedLLM([answer("Sunny.")])

    await execute_run(run.id, llm)

    system = llm.requests[0].messages[0]
    assert system["role"] == "system"
    assert "- The user lives in Dubai" in system["content"]


async def test_only_the_run_owners_memories_are_retrieved(
    authed_user: AuthedUser,
    user_factory: UserFactory,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    memory_factory: MemoryFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    await memory_factory(await user_factory(), "Someone else lives in Dubai")
    run = await agent_run_factory(await agent_session_factory(authed_user), "Dubai?")
    llm = ScriptedLLM([answer("ok")])

    await execute_run(run.id, llm)

    assert (await _retrieval(db_session, run.id))["memories"] == []
    assert "Someone else" not in llm.requests[0].messages[0]["content"]


async def test_embedding_failure_lets_the_run_continue_without_memories(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    memory_factory: MemoryFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    await memory_factory(authed_user, "The user lives in Dubai")
    run = await agent_run_factory(
        await agent_session_factory(authed_user, system_prompt=PERSONA), "Dubai?"
    )
    llm = ScriptedLLM([answer("ok")])

    await execute_run(run.id, llm, FailingEmbedder())

    assert llm.requests[0].messages[0] == {"role": "system", "content": PERSONA}
    assert [s.step_type for s in await _steps(db_session, run.id)] == [
        "memory_retrieval",
        "llm_call",
        "final_answer",
    ]
    assert (await _retrieval(db_session, run.id))["memories"] == []
    finished = await db_session.get(AgentRun, run.id, populate_existing=True)
    assert finished is not None
    assert finished.status == RunStatus.COMPLETED


async def test_a_fact_remembered_in_one_session_is_recalled_in_another(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    first = await agent_run_factory(
        await agent_session_factory(authed_user, tools_enabled=["remember_fact"]),
        "Remember that I live in Dubai.",
    )
    await execute_run(
        first.id,
        ScriptedLLM(
            [call_tool("remember_fact", {"fact": "The user lives in Dubai"}), answer("Noted.")]
        ),
    )
    second = await agent_run_factory(
        await agent_session_factory(authed_user, system_prompt=PERSONA, name="Travel"),
        "What's the weather where I live?",
    )
    llm = ScriptedLLM([answer("Sunny in Dubai.")])

    await execute_run(second.id, llm)

    assert "- The user lives in Dubai" in llm.requests[0].messages[0]["content"]
    [recalled] = (await _retrieval(db_session, second.id))["memories"]
    assert recalled["content"] == "The user lives in Dubai"
