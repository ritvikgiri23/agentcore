import json
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.llm import ChatMessage, ChatResult, LLMProvider, ToolDefinition
from app.llm.fake import ScriptedLLM, answer, call_tool
from app.models import AgentRun, AgentSession, RunStatus, RunStep
from app.runs import cancellation, lifecycle
from app.runs.events import run_channel
from app.runs.lease import RunLease
from app.tools import ToolContext, tool
from tests.conftest import (
    AgentRunFactory,
    AgentSessionFactory,
    AuthedUser,
    RunExecutor,
    UserFactory,
)

RUNS = "/api/v1/runs"
SESSIONS = "/api/v1/sessions"


async def _steps(db: AsyncSession, run_id: str) -> list[RunStep]:
    result = await db.scalars(
        select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.id)
    )
    return list(result)


async def _run(db: AsyncSession, run_id: str) -> AgentRun:
    run = await db.get(AgentRun, run_id, populate_existing=True)
    assert run is not None
    return run


async def _subscribe(redis: FakeAsyncRedis, run_id: str) -> Any:
    pubsub = redis.pubsub()
    await pubsub.subscribe(run_channel(run_id))
    return pubsub


async def _published(pubsub: Any) -> list[dict[str, Any]]:
    events = []
    while (message := await pubsub.get_message(timeout=0)) is not None:
        if message["type"] == "message":
            events.append(json.loads(message["data"]))
    await pubsub.aclose()
    return events


DONE_CANCELLED = {"step_type": "done", "status": "cancelled"}


# --- HTTP seam ---


async def test_a_cancelled_queued_run_never_executes(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
    redis: FakeAsyncRedis,
    revoked: list[str],
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    pubsub = await _subscribe(redis, run.id)

    response = await client.delete(f"{RUNS}/{run.id}", headers=authed_user.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] == run.id
    assert body["status"] == "cancelled"
    assert body["finished_at"] is not None
    assert body["step_count"] == 1
    assert revoked == [run.id]
    assert [(s.step_type, s.payload) for s in await _steps(db_session, run.id)] == [
        ("cancelled", {"reason": "cancelled by user"}),
    ]
    events = await _published(pubsub)
    assert [e["step_type"] for e in events] == ["cancelled", "done"]
    assert events[-1] == DONE_CANCELLED

    # The broker message may still reach a worker; its claim fails.
    llm = ScriptedLLM([answer("should not run")])
    await execute_run(run.id, llm)

    assert llm.requests == []
    assert (await _run(db_session, run.id)).status == RunStatus.CANCELLED
    assert len(await _steps(db_session, run.id)) == 1


async def test_cancelling_a_running_run_flags_and_revokes_its_worker(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    db_session: AsyncSession,
    redis: FakeAsyncRedis,
    revoked: list[str],
) -> None:
    run = await agent_run_factory(
        await agent_session_factory(authed_user), status=RunStatus.RUNNING
    )
    # A live worker holds it; that worker records the outcome when it stops.
    assert await RunLease(redis, run.id, ttl_seconds=60).acquire()

    response = await client.delete(f"{RUNS}/{run.id}", headers=authed_user.headers)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert (await _run(db_session, run.id)).status == RunStatus.CANCELLED
    assert await cancellation.is_requested(redis, run.id)
    assert revoked == [run.id]
    assert await _steps(db_session, run.id) == []


async def test_cancelling_a_running_run_whose_worker_is_gone_settles_it(
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
    pubsub = await _subscribe(redis, run.id)

    response = await client.delete(f"{RUNS}/{run.id}", headers=authed_user.headers)

    assert response.status_code == 200
    assert [s.step_type for s in await _steps(db_session, run.id)] == ["cancelled"]
    assert (await _published(pubsub))[-1] == DONE_CANCELLED


@pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED])
async def test_cancelling_a_finished_run_is_a_conflict(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    db_session: AsyncSession,
    revoked: list[str],
    status: RunStatus,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user), status=status)

    response = await client.delete(f"{RUNS}/{run.id}", headers=authed_user.headers)

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "conflict"
    assert error["details"] == {"status": status.value}
    assert (await _run(db_session, run.id)).status == status
    assert revoked == []
    assert await _steps(db_session, run.id) == []


async def test_cancelling_another_users_run_is_404(
    client: AsyncClient,
    user_factory: UserFactory,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    db_session: AsyncSession,
    revoked: list[str],
) -> None:
    owner, intruder = await user_factory(), await user_factory()
    run = await agent_run_factory(await agent_session_factory(owner))

    response = await client.delete(f"{RUNS}/{run.id}", headers=intruder.headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert (await _run(db_session, run.id)).status == RunStatus.QUEUED
    assert revoked == []


@pytest.mark.parametrize("run_id", ["not-a-uuid", "00000000-0000-4000-8000-000000000000"])
async def test_cancelling_an_unknown_run_is_404(
    client: AsyncClient, authed_user: AuthedUser, run_id: str
) -> None:
    response = await client.delete(f"{RUNS}/{run_id}", headers=authed_user.headers)

    assert response.status_code == 404


async def test_cancelling_requires_authentication(client: AsyncClient) -> None:
    response = await client.delete(f"{RUNS}/some-id")

    assert response.status_code == 401


async def test_deleting_a_session_cancels_its_active_runs_first(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    db_session: AsyncSession,
    redis: FakeAsyncRedis,
    revoked: list[str],
) -> None:
    agent_session = await agent_session_factory(authed_user)
    queued = await agent_run_factory(agent_session)
    running = await agent_run_factory(agent_session, status=RunStatus.RUNNING)
    assert await RunLease(redis, running.id, ttl_seconds=60).acquire()
    finished = await agent_run_factory(agent_session, status=RunStatus.COMPLETED)
    queued_events = await _subscribe(redis, queued.id)

    response = await client.delete(f"{SESSIONS}/{agent_session.id}", headers=authed_user.headers)

    assert response.status_code == 204
    assert sorted(revoked) == sorted([queued.id, running.id])
    assert finished.id not in revoked
    # The running run's worker sees the flag at its next step boundary.
    assert await cancellation.is_requested(redis, running.id)
    events = await _published(queued_events)
    assert events[0]["payload"] == {"reason": "session deleted"}
    assert events[-1] == DONE_CANCELLED
    assert await db_session.scalar(select(func.count()).select_from(AgentRun)) == 0
    assert await db_session.get(AgentSession, agent_session.id, populate_existing=True) is None


# --- Runner seam ---


async def test_a_cancel_flag_set_mid_loop_stops_the_run_at_the_next_step(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
    redis: FakeAsyncRedis,
    isolated_registry: None,
) -> None:
    revoked: list[str] = []

    @tool
    async def slow(ctx: ToolContext) -> str:
        """The user cancels while this tool runs."""
        await cancellation.request_cancel(
            db_session,
            redis,
            ctx.run_id,
            reason=cancellation.CancelReason.BY_USER,
            revoke=revoked.append,
        )
        return "done anyway"

    run = await agent_run_factory(
        await agent_session_factory(authed_user, tools_enabled=["slow"])
    )
    pubsub = await _subscribe(redis, run.id)
    llm = ScriptedLLM([call_tool("slow", {}), answer("should not be asked")])

    await execute_run(run.id, llm)

    assert len(llm.requests) == 1
    steps = await _steps(db_session, run.id)
    assert [s.step_type for s in steps] == [
        "memory_retrieval",
        "llm_call",
        "tool_call",
        "tool_result",
        "cancelled",
    ]
    assert steps[-1].payload == {"reason": "cancelled by user"}
    finished = await _run(db_session, run.id)
    assert finished.status == RunStatus.CANCELLED
    assert finished.final_answer is None
    assert revoked == [run.id]
    events = await _published(pubsub)
    assert events.count(DONE_CANCELLED) == 1
    assert events[-1] == DONE_CANCELLED


class _CancelledDuringCall:
    """Plays the script, but the run is cancelled while the given call is in flight."""

    def __init__(
        self, llm: LLMProvider, db: AsyncSession, redis: FakeAsyncRedis, run_id: str, *, flag: bool
    ) -> None:
        self._llm = llm
        self._db = db
        self._redis = redis
        self._run_id = run_id
        self._flag = flag
        self.calls = 0

    async def chat(self, messages: list[ChatMessage], tools: list[ToolDefinition]) -> ChatResult:
        self.calls += 1
        if self._flag:
            await cancellation.request_cancel(
                self._db,
                self._redis,
                self._run_id,
                reason=cancellation.CancelReason.BY_USER,
                revoke=lambda _: None,
            )
        else:
            # Only the status changes, as if the flag were lost: the conditional terminal
            # write alone must keep the run cancelled.
            await lifecycle.cancel(self._db, self._redis, self._run_id)
        return await self._llm.chat(messages, tools)

    async def aclose(self) -> None:
        return None


@pytest.mark.parametrize("flag", [True, False], ids=["flag", "status-only"])
async def test_a_run_cancelled_during_its_last_llm_call_is_never_completed(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
    redis: FakeAsyncRedis,
    flag: bool,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    pubsub = await _subscribe(redis, run.id)
    llm = _CancelledDuringCall(
        ScriptedLLM([answer("Too late.")]), db_session, redis, run.id, flag=flag
    )

    await execute_run(run.id, llm)

    finished = await _run(db_session, run.id)
    assert finished.status == RunStatus.CANCELLED
    assert finished.final_answer is None
    step_types = [s.step_type for s in await _steps(db_session, run.id)]
    assert step_types[-1] == "cancelled"
    assert step_types.count("cancelled") == 1
    if flag:
        # The flag is checked before the answer is recorded.
        assert "final_answer" not in step_types
    events = await _published(pubsub)
    assert events[-1] == DONE_CANCELLED
    assert {"step_type": "done", "status": "completed"} not in events


async def test_a_run_whose_row_is_deleted_mid_loop_stops_quietly(
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
    redis: FakeAsyncRedis,
    isolated_registry: None,
) -> None:
    agent_session = await agent_session_factory(authed_user, tools_enabled=["vanish"])

    @tool
    async def vanish(ctx: ToolContext) -> str:
        """The session, and with it the run, is deleted while this tool runs."""
        await db_session.delete(agent_session)
        await db_session.commit()
        return "gone"

    run = await agent_run_factory(agent_session)
    pubsub = await _subscribe(redis, run.id)
    llm = ScriptedLLM([call_tool("vanish", {}), answer("should not be asked")])

    with capture_logs() as logs:
        await execute_run(run.id, llm)

    assert len(llm.requests) == 1
    assert await db_session.get(AgentRun, run.id, populate_existing=True) is None
    assert not [log for log in logs if log["log_level"] in ("error", "exception")]
    assert "run_failed" not in [log["event"] for log in logs]
    assert (await _published(pubsub))[-1] == DONE_CANCELLED
