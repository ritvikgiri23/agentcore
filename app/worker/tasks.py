import asyncio

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.llm import create_embedder, create_llm_provider
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


async def _execute(run_id: str) -> None:
    # asyncpg connections and the Redis/HTTP clients are bound to the event loop that
    # created them, and each task gets a fresh loop, so nothing is shared across tasks.
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
    llm = create_llm_provider(settings)
    embedder = create_embedder(settings)
    try:
        await run_agent(
            run_id,
            RunnerDeps(
                session_factory=async_sessionmaker(engine, expire_on_commit=False),
                redis=redis,
                llm=llm,
                embedder=embedder,
            ),
        )
    finally:
        await embedder.aclose()
        await llm.aclose()
        await redis.aclose()
        await engine.dispose()


def enqueue_run(run_id: str) -> None:
    """Hand a run to the broker. Blocking: call it from a worker thread."""
    execute_agent_run.apply_async(args=[run_id], task_id=run_id)
