import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from celery.exceptions import SoftTimeLimitExceeded
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.llm import create_embedder, create_llm_provider
from app.runs import cancellation
from app.runs.runner import RunInProgress, RunnerDeps, run_agent
from app.worker.celery_app import celery_app

logger = structlog.get_logger(__name__)


# Retries anything that escapes the runner: an outage before the claim, or one that kept
# a claimed run from being finished, which the retry then fails as worker lost.
RETRY_LIMIT = 5
RETRY_BACKOFF_MAX_SECONDS = 60
# Slack past the lease TTL, so the next check finds the lease expired if its holder died.
LEASE_RECHECK_SLACK_SECONDS = 1


@celery_app.task(  # type: ignore[untyped-decorator]
    name="agentcore.execute_agent_run",
    autoretry_for=(Exception,),
    max_retries=RETRY_LIMIT,
    retry_backoff=True,
    retry_backoff_max=RETRY_BACKOFF_MAX_SECONDS,
    retry_jitter=True,
)
def execute_agent_run(run_id: str) -> None:
    """Execute one agent run. The task id is the run id."""
    try:
        asyncio.run(_execute(run_id))
    except RunInProgress as exc:
        # The holder may have just died (its process was killed and this is the requeued
        # message), so check back rather than drop the run. A live holder keeps its lease
        # fresh until the run is finished; the check after that is a no-op. A fresh
        # message rather than a retry, so these checks don't use up the retry budget.
        execute_agent_run.apply_async(
            args=[run_id],
            task_id=run_id,
            countdown=exc.retry_in + LEASE_RECHECK_SLACK_SECONDS,
        )
    except SoftTimeLimitExceeded:
        # Raised by a revoke's SIGUSR1 while the run was blocked inside a call. The loop
        # it ran on is gone, so the run is settled on a fresh, short one.
        logger.info("run_terminated", run_id=run_id)
        asyncio.run(_settle_cancelled(run_id))


@asynccontextmanager
async def _connections() -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], Redis]]:
    # asyncpg connections and the Redis/HTTP clients are bound to the event loop that
    # created them, and each task gets a fresh loop, so nothing is shared across tasks.
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False), redis
    finally:
        await redis.aclose()
        await engine.dispose()


async def _execute(run_id: str) -> None:
    settings = get_settings()
    llm = create_llm_provider(settings)
    embedder = create_embedder(settings)
    try:
        async with _connections() as (session_factory, redis):
            await run_agent(
                run_id,
                RunnerDeps(
                    session_factory=session_factory,
                    redis=redis,
                    llm=llm,
                    embedder=embedder,
                    interrupts=(SoftTimeLimitExceeded,),
                ),
            )
    finally:
        await embedder.aclose()
        await llm.aclose()


async def _settle_cancelled(run_id: str) -> None:
    async with _connections() as (session_factory, redis), session_factory() as db:
        await cancellation.settle(db, redis, run_id)


def enqueue_run(run_id: str) -> None:
    """Hand a run to the broker. Blocking: call it from a worker thread."""
    execute_agent_run.apply_async(args=[run_id], task_id=run_id)


def revoke_run(run_id: str) -> None:
    """Revoke a run's task. Blocking: call it from a worker thread.

    A task not yet started is dropped; one executing gets SIGUSR1, which Celery raises
    in it as `SoftTimeLimitExceeded`.
    """
    celery_app.control.revoke(run_id, terminate=True, signal="SIGUSR1")
