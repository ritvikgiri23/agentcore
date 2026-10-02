import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.llm.fake import HashEmbedder
from app.models import AgentRun, LongTermMemory
from app.tools import ToolContext, dispatch
from tests.conftest import (
    AgentRunFactory,
    AgentSessionFactory,
    AuthedUser,
    FailingEmbedder,
    UserFactory,
)

ENABLED = ["remember_fact"]


@pytest.fixture
async def run(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
) -> AgentRun:
    return await agent_run_factory(await agent_session_factory(authed_user, tools_enabled=ENABLED))


@pytest.fixture
def ctx(
    authed_user: AuthedUser,
    run: AgentRun,
    worker_session_factory: async_sessionmaker[AsyncSession],
) -> ToolContext:
    return ToolContext(
        user_id=authed_user.user.id,
        run_id=run.id,
        session_factory=worker_session_factory,
        embedder=HashEmbedder(),
    )


async def _memories(db: AsyncSession, user_id: str) -> list[LongTermMemory]:
    result = await db.scalars(
        select(LongTermMemory)
        .where(LongTermMemory.user_id == user_id)
        .order_by(LongTermMemory.created_at)
    )
    return list(result)


async def test_remember_fact_stores_the_fact_with_its_source_run(
    ctx: ToolContext, run: AgentRun, authed_user: AuthedUser, db_session: AsyncSession
) -> None:
    outcome = await dispatch(
        "remember_fact", '{"fact": "The user lives in Dubai."}', ENABLED, ctx
    )

    assert (outcome.result, outcome.is_error) == ("Remembered: The user lives in Dubai.", False)
    [memory] = await _memories(db_session, authed_user.user.id)
    assert memory.content == "The user lives in Dubai."
    assert memory.source_run_id == run.id


async def test_near_duplicate_fact_is_not_stored_again(
    ctx: ToolContext, authed_user: AuthedUser, db_session: AsyncSession
) -> None:
    await dispatch("remember_fact", '{"fact": "The user lives in Dubai."}', ENABLED, ctx)

    # Same words, different case and punctuation: the same embedding.
    outcome = await dispatch("remember_fact", '{"fact": "the user lives in dubai"}', ENABLED, ctx)

    assert outcome.result == "Already remembered: The user lives in Dubai."
    assert outcome.is_error is False
    assert [m.content for m in await _memories(db_session, authed_user.user.id)] == [
        "The user lives in Dubai."
    ]


async def test_distinct_facts_are_both_stored(
    ctx: ToolContext, authed_user: AuthedUser, db_session: AsyncSession
) -> None:
    await dispatch("remember_fact", '{"fact": "The user lives in Dubai."}', ENABLED, ctx)

    outcome = await dispatch(
        "remember_fact", '{"fact": "The user prefers metric units."}', ENABLED, ctx
    )

    assert outcome.result == "Remembered: The user prefers metric units."
    assert len(await _memories(db_session, authed_user.user.id)) == 2


async def test_another_users_memory_does_not_count_as_a_duplicate(
    ctx: ToolContext,
    authed_user: AuthedUser,
    user_factory: UserFactory,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    worker_session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    other = await user_factory()
    other_run = await agent_run_factory(await agent_session_factory(other, tools_enabled=ENABLED))
    other_ctx = ToolContext(
        user_id=other.user.id,
        run_id=other_run.id,
        session_factory=worker_session_factory,
        embedder=HashEmbedder(),
    )
    await dispatch("remember_fact", '{"fact": "Lives in Dubai."}', ENABLED, other_ctx)

    outcome = await dispatch("remember_fact", '{"fact": "Lives in Dubai."}', ENABLED, ctx)

    assert outcome.result == "Remembered: Lives in Dubai."
    assert len(await _memories(db_session, authed_user.user.id)) == 1


async def test_embedding_failure_is_an_error_result(
    ctx: ToolContext, authed_user: AuthedUser, db_session: AsyncSession
) -> None:
    ctx.embedder = FailingEmbedder()

    outcome = await dispatch("remember_fact", '{"fact": "Lives in Dubai."}', ENABLED, ctx)

    assert outcome.is_error is True
    assert outcome.result == "Error in 'remember_fact': RuntimeError: embedding service down"
    assert await _memories(db_session, authed_user.user.id) == []
