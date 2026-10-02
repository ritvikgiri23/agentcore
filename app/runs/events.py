import json
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import RunStatus, RunStep, StepType

logger = structlog.get_logger(__name__)


def run_channel(run_id: str) -> str:
    """The Redis pub/sub channel carrying a run's live events."""
    return f"run:{run_id}:events"


def step_event(step: RunStep) -> dict[str, Any]:
    return {
        "id": step.id,
        "step_type": step.step_type,
        "payload": step.payload,
        "occurred_at": step.occurred_at.isoformat(),
    }


class EventPublisher:
    """Persists run steps, then publishes them live.

    Persist-before-publish is an invariant: Postgres is the source of truth and pub/sub
    only the live tail, so a subscriber can always find a published step in the database.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], redis: Redis) -> None:
        self._session_factory = session_factory
        self._redis = redis

    async def record(self, run_id: str, step_type: StepType, payload: dict[str, Any]) -> RunStep:
        async with self._session_factory() as db:
            step = RunStep(run_id=run_id, step_type=step_type.value, payload=payload)
            db.add(step)
            await db.commit()
            # Loads the server-generated occurrence time.
            await db.refresh(step)
        with structlog.contextvars.bound_contextvars(step_type=step_type.value):
            logger.info("run_step_recorded", step_id=step.id)
        await self._redis.publish(run_channel(run_id), json.dumps(step_event(step)))
        return step

    async def publish_done(self, run_id: str, status: RunStatus) -> None:
        await publish_done(self._redis, run_id, status)


async def publish_done(redis: Redis, run_id: str, status: RunStatus) -> None:
    """The terminal event: tells live subscribers the run is over. Never persisted."""
    await redis.publish(
        run_channel(run_id), json.dumps({"step_type": "done", "status": status.value})
    )
