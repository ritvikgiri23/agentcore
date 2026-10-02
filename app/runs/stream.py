"""Replay-then-live delivery of a run's events, for the SSE stream."""

import json
import time
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager, suppress
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TERMINAL_STATUSES, AgentRun, RunStatus, RunStep
from app.runs.events import DONE, done_event, run_channel, step_event

# How long a stream may sit without live events before it asks Postgres whether the run
# ended anyway, e.g. because its `done` event was lost.
STATUS_RECHECK_SECONDS = 15.0


@asynccontextmanager
async def subscribe(redis: Redis, run_id: str) -> AsyncIterator[PubSub]:
    """A subscription to the run's channel; live events buffer until they are read."""
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(run_channel(run_id))
        yield pubsub
    finally:
        try:
            # Dropping the connection unsubscribes too, so a failure here can be ignored.
            with suppress(RedisError):
                await pubsub.unsubscribe()
        finally:
            await pubsub.aclose()  # type: ignore[no-untyped-call]


async def run_events(
    db: AsyncSession, pubsub: PubSub, run_id: str
) -> AsyncIterator[dict[str, Any]]:
    """Every step of the run exactly once, then a `done` event.

    Steps arrive in the order they happened; the only exception is a step whose live
    publish was lost, which is recovered from Postgres just before `done`.

    `pubsub` must already be subscribed. Anything published from then on is buffered,
    so whatever the replay below misses is still waiting in the subscription.
    """
    sent: set[int] = set()
    # Status before steps: once a run is terminal, all of its steps are persisted.
    status = await _status(db, run_id)
    for event in await _steps(db, run_id, exclude=sent):
        sent.add(event["id"])
        yield event
    if status in TERMINAL_STATUSES:
        yield done_event(status)
        return

    last_activity = time.monotonic()
    while status not in TERMINAL_STATUSES:
        idle = time.monotonic() - last_activity
        message = await pubsub.get_message(
            ignore_subscribe_messages=True, timeout=max(STATUS_RECHECK_SECONDS - idle, 0)
        )
        if message is None:
            # Also returned for ignored subscribe confirmations, not only after a timeout.
            if time.monotonic() - last_activity < STATUS_RECHECK_SECONDS:
                continue
            # A deleted run is over as far as anyone watching is concerned.
            status = await _status(db, run_id) or RunStatus.CANCELLED
            last_activity = time.monotonic()
            continue
        last_activity = time.monotonic()
        event = json.loads(message["data"])
        if event["step_type"] == DONE:
            status = RunStatus(event["status"])
        elif event["id"] not in sent:
            sent.add(event["id"])
            yield event

    # A step whose live publish failed is still in Postgres; send it before `done`.
    for event in await _steps(db, run_id, exclude=sent):
        yield event
    yield done_event(status)


async def _status(db: AsyncSession, run_id: str) -> RunStatus | None:
    status = await db.scalar(select(AgentRun.status).where(AgentRun.id == run_id))
    # End the read transaction so a long-lived stream does not pin a pooled connection.
    await db.commit()
    return status


async def _steps(
    db: AsyncSession, run_id: str, *, exclude: Collection[int]
) -> list[dict[str, Any]]:
    steps = await db.scalars(
        select(RunStep)
        .where(RunStep.run_id == run_id)
        .order_by(RunStep.occurred_at, RunStep.id)
    )
    events = [step_event(s) for s in steps if s.id not in exclude]
    await db.commit()
    return events
