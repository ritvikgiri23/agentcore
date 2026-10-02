from fakeredis import FakeAsyncRedis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


async def test_test_database_has_pgvector(db_session: AsyncSession) -> None:
    distance = await db_session.scalar(text("SELECT '[1,0]'::vector <=> '[0,1]'::vector"))

    assert distance == 1.0


async def test_commits_inside_a_test_are_rolled_back_afterwards(
    engine: AsyncEngine, db_session: AsyncSession
) -> None:
    await db_session.execute(text("CREATE TABLE harness_probe (id int)"))
    await db_session.commit()
    assert await db_session.scalar(text("SELECT count(*) FROM harness_probe")) == 0

    # An independent connection must not see the uncommitted outer transaction.
    async with engine.connect() as other:
        visible = await other.scalar(text("SELECT to_regclass('harness_probe') IS NOT NULL"))
    assert visible is False


async def test_redis_fixture_is_usable(redis: FakeAsyncRedis) -> None:
    await redis.rpush("k", "a", "b")  # type: ignore[misc]

    assert await redis.lrange("k", 0, -1) == ["a", "b"]  # type: ignore[misc]
