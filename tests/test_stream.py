import asyncio
import json
import logging
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from httpx import AsyncClient
from redis.asyncio.client import PubSub
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Message

from app.core.redis import get_redis
from app.llm.fake import ScriptedLLM, answer, call_tool
from app.models import AgentRun, RunStatus, RunStep, StepType
from app.runs import stream
from app.runs.events import run_channel, step_event
from tests.conftest import (
    AgentRunFactory,
    AgentSessionFactory,
    AuthedUser,
    RunExecutor,
    UserFactory,
)

RUNS = "/api/v1/runs"


def _stream_url(run_id: str) -> str:
    return f"{RUNS}/{run_id}/stream"


def _frames(body: str) -> list[dict[str, Any]]:
    """The JSON payload of every `data:` frame, in order; comments are skipped."""
    frames = []
    for block in body.split("\n\n"):
        data = [line.removeprefix("data: ") for line in block.split("\n") if line.startswith("data: ")]
        if data:
            frames.append(json.loads("\n".join(data)))
    return frames


async def _persisted_events(db: AsyncSession, run_id: str) -> list[dict[str, Any]]:
    steps = await db.scalars(
        select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.id)
    )
    return [step_event(s) for s in steps]


async def _add_step(db: AsyncSession, run_id: str, step_type: StepType, **payload: Any) -> RunStep:
    step = RunStep(run_id=run_id, step_type=step_type.value, payload=payload)
    db.add(step)
    await db.commit()
    await db.refresh(step)
    return step


async def _wait_for_subscriber(redis: FakeAsyncRedis, run_id: str) -> None:
    async with asyncio.timeout(2):
        while (await redis.pubsub_numsub(run_channel(run_id)))[0][1] == 0:
            await asyncio.sleep(0.005)


async def _publish(redis: FakeAsyncRedis, run_id: str, event: dict[str, Any]) -> None:
    await redis.publish(run_channel(run_id), json.dumps(event))


async def test_finished_run_replays_every_step_then_done(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    await execute_run(
        run.id,
        ScriptedLLM([call_tool("calculator", {"expression": "6 * 7"}), answer("42")]),
    )

    response = await client.get(_stream_url(run.id), headers=authed_user.headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    persisted = await _persisted_events(db_session, run.id)
    assert [e["step_type"] for e in persisted] == [
        "memory_retrieval",
        "llm_call",
        "tool_call",
        "tool_result",
        "llm_call",
        "final_answer",
    ]
    assert _frames(response.text) == [*persisted, {"step_type": "done", "status": "completed"}]


async def test_frames_are_single_data_lines(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    await execute_run(run.id, ScriptedLLM([answer("line one\nline two")]))

    response = await client.get(_stream_url(run.id), headers=authed_user.headers)

    *steps, done = [block for block in response.text.split("\n\n") if block]
    assert all(block.startswith("data: {") and "\n" not in block for block in steps)
    assert done == 'data: {"step_type": "done", "status": "completed"}'


async def test_failed_run_ends_with_done_failed(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    # An empty script makes the provider raise on the first call.
    await execute_run(run.id, ScriptedLLM())

    response = await client.get(_stream_url(run.id), headers=authed_user.headers)

    frames = _frames(response.text)
    assert [f["step_type"] for f in frames] == ["memory_retrieval", "error", "done"]
    assert frames[-1] == {"step_type": "done", "status": "failed"}


async def test_in_progress_run_replays_then_relays_live_steps_without_duplicates(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    db_session: AsyncSession,
    redis: FakeAsyncRedis,
) -> None:
    run = await agent_run_factory(
        await agent_session_factory(authed_user), status=RunStatus.RUNNING
    )
    first = step_event(await _add_step(db_session, run.id, StepType.LLM_CALL, iteration=1))
    second = step_event(await _add_step(db_session, run.id, StepType.TOOL_CALL, name="calculator"))
    # Published live only, standing in for a step persisted after the replay ran; nothing
    # here may touch the database while the stream is using the shared connection.
    third = {**second, "id": second["id"] + 1, "step_type": "tool_result", "payload": {"x": 1}}

    async def worker() -> None:
        await _wait_for_subscriber(redis, run.id)
        # Persisted before the stream replayed, and published after it subscribed.
        await _publish(redis, run.id, second)
        await _publish(redis, run.id, third)
        await _publish(redis, run.id, {"step_type": "done", "status": "completed"})

    publishing = asyncio.create_task(worker())
    response = await client.get(_stream_url(run.id), headers=authed_user.headers)
    await publishing

    assert _frames(response.text) == [
        first,
        second,
        third,
        {"step_type": "done", "status": "completed"},
    ]


class _SignallingPubSub(PubSub):
    """Signals each time the stream starts waiting for a live message."""

    waiting: asyncio.Event

    async def get_message(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        self.waiting.set()
        message: dict[str, Any] | None = await super().get_message(*args, **kwargs)
        return message


class _SignallingRedis(FakeAsyncRedis):
    def __init__(self) -> None:
        super().__init__(decode_responses=True)
        self.waiting = asyncio.Event()

    def pubsub(self, **kwargs: Any) -> PubSub:
        pubsub = _SignallingPubSub(self.connection_pool, **kwargs)
        pubsub.waiting = self.waiting
        return pubsub


async def test_steps_whose_live_event_was_lost_are_sent_before_done(
    app: FastAPI,
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    db_session: AsyncSession,
) -> None:
    redis = _SignallingRedis()
    app.dependency_overrides[get_redis] = lambda: redis
    run = await agent_run_factory(
        await agent_session_factory(authed_user), status=RunStatus.RUNNING
    )
    lost: dict[str, Any] = {}

    async def worker() -> None:
        # The stream has replayed and is idle, so its database session is free.
        await redis.waiting.wait()
        # Persisted after the replay, but its publish never reached Redis.
        lost.update(step_event(await _add_step(db_session, run.id, StepType.FINAL_ANSWER)))
        await _publish(redis, run.id, {"step_type": "done", "status": "completed"})

    try:
        publishing = asyncio.create_task(worker())
        response = await client.get(_stream_url(run.id), headers=authed_user.headers)
        await publishing
    finally:
        await redis.aclose()

    assert _frames(response.text) == [lost, {"step_type": "done", "status": "completed"}]


async def test_idle_stream_rechecks_the_run_and_ends_when_done_was_missed(
    app: FastAPI,
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream, "STATUS_RECHECK_SECONDS", 0.3)
    redis = _SignallingRedis()
    app.dependency_overrides[get_redis] = lambda: redis
    run = await agent_run_factory(
        await agent_session_factory(authed_user), status=RunStatus.RUNNING
    )
    first = step_event(await _add_step(db_session, run.id, StepType.LLM_CALL, iteration=1))
    final: dict[str, Any] = {}

    async def worker() -> None:
        # The stream has replayed and is idle, so its database session is free.
        await redis.waiting.wait()
        final.update(
            step_event(await _add_step(db_session, run.id, StepType.FINAL_ANSWER, content="x"))
        )
        stored = await db_session.get(AgentRun, run.id)
        assert stored is not None
        stored.status = RunStatus.COMPLETED
        await db_session.commit()
        # No `done` is ever published: the worker died after finishing, say.

    try:
        finishing = asyncio.create_task(worker())
        response = await client.get(_stream_url(run.id), headers=authed_user.headers)
        await finishing
    finally:
        await redis.aclose()

    assert _frames(response.text) == [first, final, {"step_type": "done", "status": "completed"}]


async def test_stream_of_a_run_deleted_mid_flight_ends_with_done_cancelled(
    app: FastAPI,
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream, "STATUS_RECHECK_SECONDS", 0.3)
    redis = _SignallingRedis()
    app.dependency_overrides[get_redis] = lambda: redis
    run = await agent_run_factory(
        await agent_session_factory(authed_user), status=RunStatus.RUNNING
    )

    async def delete_run() -> None:
        await redis.waiting.wait()
        await db_session.execute(delete(AgentRun).where(AgentRun.id == run.id))
        await db_session.commit()

    try:
        deleting = asyncio.create_task(delete_run())
        response = await client.get(_stream_url(run.id), headers=authed_user.headers)
        await deleting
    finally:
        await redis.aclose()

    assert _frames(response.text) == [{"step_type": "done", "status": "cancelled"}]


async def test_idle_stream_sends_keepalive_comments(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    redis: FakeAsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("fastapi.routing._PING_INTERVAL", 0.01)
    run = await agent_run_factory(
        await agent_session_factory(authed_user), status=RunStatus.RUNNING
    )

    async def worker() -> None:
        await _wait_for_subscriber(redis, run.id)
        await asyncio.sleep(0.1)
        await _publish(redis, run.id, {"step_type": "done", "status": "completed"})

    publishing = asyncio.create_task(worker())
    response = await client.get(_stream_url(run.id), headers=authed_user.headers)
    await publishing

    assert ": ping\n\n" in response.text
    assert _frames(response.text) == [{"step_type": "done", "status": "completed"}]


async def test_access_token_query_parameter_is_accepted_and_not_logged(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    await execute_run(run.id, ScriptedLLM([answer("Hi.")]))

    with caplog.at_level(logging.DEBUG):
        response = await client.get(
            _stream_url(run.id), params={"access_token": authed_user.token}
        )

    assert response.status_code == 200
    assert _frames(response.text)[-1] == {"step_type": "done", "status": "completed"}
    # httpx logs the URL it requests; that is this test's client, not the server.
    server_records = [r for r in caplog.records if r.name != "httpx"]
    assert server_records
    assert all(authed_user.token not in r.getMessage() for r in server_records)


async def test_query_parameter_token_is_only_accepted_by_the_stream(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))

    response = await client.get(
        f"{RUNS}/{run.id}/status", params={"access_token": authed_user.token}
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("headers", "params"),
    [
        ({}, {}),
        ({}, {"access_token": "not-a-jwt"}),
        ({"Authorization": "Bearer not-a-jwt"}, {}),
    ],
    ids=["missing", "bad-query-token", "bad-header-token"],
)
async def test_stream_requires_a_valid_token(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    headers: dict[str, str],
    params: dict[str, str],
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))

    response = await client.get(_stream_url(run.id), headers=headers, params=params)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_another_users_run_is_404(
    client: AsyncClient,
    authed_user: AuthedUser,
    user_factory: UserFactory,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    redis: FakeAsyncRedis,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    intruder = await user_factory()

    response = await client.get(_stream_url(run.id), headers=intruder.headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    # Ownership is checked before subscribing.
    assert (await redis.pubsub_numsub(run_channel(run.id)))[0][1] == 0


@pytest.mark.parametrize("run_id", ["00000000-0000-4000-8000-000000000000", "not-a-uuid"])
async def test_unknown_run_is_404(
    client: AsyncClient, authed_user: AuthedUser, run_id: str
) -> None:
    response = await client.get(_stream_url(run_id), headers=authed_user.headers)

    assert response.status_code == 404


async def test_client_disconnect_unsubscribes_quietly(
    app: FastAPI,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    db_session: AsyncSession,
    redis: FakeAsyncRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = await agent_run_factory(
        await agent_session_factory(authed_user), status=RunStatus.RUNNING
    )
    first = step_event(await _add_step(db_session, run.id, StepType.LLM_CALL, iteration=1))
    path = _stream_url(run.id)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"authorization", f"Bearer {authed_user.token}".encode()),
        ],
        "client": ("test", 1234),
        "server": ("test", 80),
    }
    disconnected = asyncio.Event()
    requested = False
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and b"data: " in message.get("body", b""):
            # The client goes away after the first frame, mid-run.
            disconnected.set()

    with caplog.at_level(logging.INFO):
        async with asyncio.timeout(2):
            await app(scope, receive, send)

    assert sent[0]["status"] == 200
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert _frames(body.decode()) == [first]
    assert (await redis.pubsub_numsub(run_channel(run.id)))[0][1] == 0
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
