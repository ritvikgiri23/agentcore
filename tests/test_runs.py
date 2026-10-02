import json
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_run_enqueuer
from app.llm.fake import ScriptedLLM, answer, call_tool
from app.models import AgentRun, AgentSession, RunStatus, RunStep
from tests.conftest import (
    AgentRunFactory,
    AgentSessionFactory,
    AuthedUser,
    RunExecutor,
    UserFactory,
)

SESSIONS = "/api/v1/sessions"
RUNS = "/api/v1/runs"
QUESTION = "What is 15% of 3,655,000?"


def _percent_script() -> ScriptedLLM:
    return ScriptedLLM(
        [
            call_tool("calculator", {"expression": "3655000 * 15 / 100"}),
            answer("15% of 3,655,000 is 548,250."),
        ]
    )


async def _submit(
    client: AsyncClient, user: AuthedUser, session_id: str, message: str = QUESTION
) -> dict[str, Any]:
    response = await client.post(
        f"{SESSIONS}/{session_id}/run", json={"message": message}, headers=user.headers
    )
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_submit_returns_run_id_and_links_and_enqueues(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    enqueued: list[str],
) -> None:
    agent_session = await agent_session_factory(authed_user)

    body = await _submit(client, authed_user, agent_session.id)

    run_id = body["run_id"]
    assert body == {
        "run_id": run_id,
        "status": "queued",
        "status_url": f"{RUNS}/{run_id}/status",
        "stream_url": f"{RUNS}/{run_id}/stream",
    }
    assert enqueued == [run_id]


@pytest.mark.parametrize("message", ["", "m" * 8001, "nul\x00byte"])
async def test_submit_validates_message(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    enqueued: list[str],
    message: str,
) -> None:
    agent_session = await agent_session_factory(authed_user)

    response = await client.post(
        f"{SESSIONS}/{agent_session.id}/run", json={"message": message}, headers=authed_user.headers
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert enqueued == []


async def test_submit_accepts_maximum_message(
    client: AsyncClient, authed_user: AuthedUser, agent_session_factory: AgentSessionFactory
) -> None:
    agent_session = await agent_session_factory(authed_user)

    await _submit(client, authed_user, agent_session.id, message="m" * 8000)


async def test_submit_to_another_users_session_is_404(
    client: AsyncClient,
    user_factory: UserFactory,
    agent_session_factory: AgentSessionFactory,
    enqueued: list[str],
) -> None:
    owner, intruder = await user_factory(), await user_factory()
    agent_session = await agent_session_factory(owner)

    response = await client.post(
        f"{SESSIONS}/{agent_session.id}/run", json={"message": "hi"}, headers=intruder.headers
    )

    assert response.status_code == 404
    assert enqueued == []


async def test_enqueue_failure_marks_run_failed(
    app: FastAPI,
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    db_session: AsyncSession,
    redis: FakeAsyncRedis,
) -> None:
    pubsub = redis.pubsub()
    await pubsub.psubscribe("run:*")
    await pubsub.get_message(timeout=0)  # the subscribe confirmation

    def broken_broker(run_id: str) -> None:
        raise ConnectionError("broker unreachable")

    app.dependency_overrides[get_run_enqueuer] = lambda: broken_broker
    agent_session = await agent_session_factory(authed_user)

    response = await client.post(
        f"{SESSIONS}/{agent_session.id}/run", json={"message": "hi"}, headers=authed_user.headers
    )

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "service_unavailable"
    run = await db_session.get(AgentRun, error["details"]["run_id"], populate_existing=True)
    assert run is not None
    assert run.status == RunStatus.FAILED
    assert run.finished_at is not None
    message = await pubsub.get_message(timeout=0)
    await pubsub.aclose()  # type: ignore[no-untyped-call]
    assert message is not None
    assert json.loads(message["data"]) == {"step_type": "done", "status": "failed"}


async def test_status_before_and_after_execution(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    execute_run: RunExecutor,
) -> None:
    agent_session = await agent_session_factory(authed_user)
    run_id = (await _submit(client, authed_user, agent_session.id))["run_id"]

    before = (await client.get(f"{RUNS}/{run_id}/status", headers=authed_user.headers)).json()
    assert before["run_id"] == run_id
    assert before["status"] == "queued"
    assert before["tokens_used"] == 0
    assert before["step_count"] == 0
    assert before["created_at"]
    assert before["started_at"] is None
    assert before["finished_at"] is None
    assert before["final_answer"] is None

    await execute_run(run_id, _percent_script())

    response = await client.get(f"{RUNS}/{run_id}/status", headers=authed_user.headers)
    assert response.status_code == 200
    after = response.json()
    assert after["status"] == "completed"
    # Two scripted LLM calls at 15 tokens each.
    assert after["tokens_used"] == 30
    assert after["step_count"] == 6
    assert after["final_answer"] == "15% of 3,655,000 is 548,250."
    assert after["created_at"] <= after["started_at"] <= after["finished_at"]


async def test_step_trace(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    execute_run: RunExecutor,
) -> None:
    agent_session = await agent_session_factory(authed_user)
    run_id = (await _submit(client, authed_user, agent_session.id))["run_id"]
    await execute_run(run_id, _percent_script())

    response = await client.get(f"{RUNS}/{run_id}/steps", headers=authed_user.headers)

    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 6
    steps = page["items"]
    assert [s["step_type"] for s in steps] == [
        "memory_retrieval",
        "llm_call",
        "tool_call",
        "tool_result",
        "llm_call",
        "final_answer",
    ]
    assert all(s["id"] and s["occurred_at"] for s in steps)
    retrieval, first_llm, tool_call, tool_result, second_llm, final = (
        s["payload"] for s in steps
    )
    assert retrieval == {"memories": [], "short_term_turns": 0}
    call_id = first_llm["tool_calls"][0]["id"]
    assert first_llm == {
        "iteration": 1,
        "model": "fake-llm",
        "finish_reason": "tool_calls",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "name": "calculator",
                "arguments": '{"expression": "3655000 * 15 / 100"}',
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "latency_ms": first_llm["latency_ms"],
    }
    assert tool_call == {
        "iteration": 1,
        "tool_call_id": call_id,
        "name": "calculator",
        "arguments": '{"expression": "3655000 * 15 / 100"}',
    }
    assert tool_result["tool_call_id"] == call_id
    assert tool_result["name"] == "calculator"
    assert tool_result["result"] == "548250"
    assert tool_result["is_error"] is False
    assert tool_result["truncated"] is False
    assert tool_result["latency_ms"] >= 0
    assert second_llm["iteration"] == 2
    assert second_llm["finish_reason"] == "stop"
    assert second_llm["tool_calls"] == []
    assert final == {
        "content": "15% of 3,655,000 is 548,250.",
        "iterations": 2,
        "max_iterations_hit": False,
    }


async def test_steps_are_paginated_in_occurrence_order(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    execute_run: RunExecutor,
) -> None:
    agent_session = await agent_session_factory(authed_user)
    run_id = (await _submit(client, authed_user, agent_session.id))["run_id"]
    await execute_run(run_id, _percent_script())

    response = await client.get(
        f"{RUNS}/{run_id}/steps", params={"limit": 2, "offset": 2}, headers=authed_user.headers
    )

    page = response.json()
    assert (page["total"], page["limit"], page["offset"]) == (6, 2, 2)
    assert [s["step_type"] for s in page["items"]] == ["tool_call", "tool_result"]


async def test_session_detail_lists_five_most_recent_runs(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
) -> None:
    agent_session = await agent_session_factory(authed_user)
    other_session = await agent_session_factory(authed_user)
    await agent_run_factory(other_session, "not this session")
    runs = [await agent_run_factory(agent_session, f"message {i}") for i in range(6)]
    long_run = await agent_run_factory(agent_session, "x" * 500, status=RunStatus.COMPLETED)

    response = await client.get(f"{SESSIONS}/{agent_session.id}", headers=authed_user.headers)

    assert response.status_code == 200
    recent = response.json()["recent_runs"]
    assert [r["id"] for r in recent] == [long_run.id, *[r.id for r in reversed(runs[2:])]]
    assert recent[0]["message"] == "x" * 120
    assert recent[0]["status"] == "completed"
    assert recent[1] == {
        "id": runs[5].id,
        "status": "queued",
        "message": "message 5",
        "tokens_used": 0,
        "created_at": recent[1]["created_at"],
        "finished_at": None,
    }


async def test_deleting_a_session_cascades_to_runs_and_steps(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    execute_run: RunExecutor,
    db_session: AsyncSession,
) -> None:
    agent_session = await agent_session_factory(authed_user)
    run_id = (await _submit(client, authed_user, agent_session.id))["run_id"]
    await execute_run(run_id, _percent_script())

    response = await client.delete(f"{SESSIONS}/{agent_session.id}", headers=authed_user.headers)

    assert response.status_code == 204
    assert await db_session.scalar(select(func.count()).select_from(AgentRun)) == 0
    assert await db_session.scalar(select(func.count()).select_from(RunStep)) == 0
    assert await db_session.get(AgentSession, agent_session.id, populate_existing=True) is None


@pytest.mark.parametrize("route", ["status", "steps"])
async def test_another_users_run_is_404(
    client: AsyncClient,
    user_factory: UserFactory,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    route: str,
) -> None:
    owner, intruder = await user_factory(), await user_factory()
    run = await agent_run_factory(await agent_session_factory(owner))

    response = await client.get(f"{RUNS}/{run.id}/{route}", headers=intruder.headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert (await client.get(f"{RUNS}/{run.id}/{route}", headers=owner.headers)).status_code == 200


@pytest.mark.parametrize("route", ["status", "steps"])
@pytest.mark.parametrize("run_id", ["not-a-uuid", "00000000-0000-4000-8000-000000000000"])
async def test_unknown_run_is_404(
    client: AsyncClient, authed_user: AuthedUser, route: str, run_id: str
) -> None:
    response = await client.get(f"{RUNS}/{run_id}/{route}", headers=authed_user.headers)

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", f"{SESSIONS}/some-id/run"),
        ("GET", f"{RUNS}/some-id/status"),
        ("GET", f"{RUNS}/some-id/steps"),
    ],
)
async def test_run_routes_require_authentication(
    client: AsyncClient, method: str, path: str
) -> None:
    response = await client.request(
        method, path, json={"message": "hi"} if method == "POST" else None
    )

    assert response.status_code == 401
