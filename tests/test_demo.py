import json
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from app.core.config import Settings, get_settings
from app.llm import create_embedder, create_llm_provider
from app.llm.demo import FLAGSHIP_PROMPT
from tests.conftest import AuthedUser, RunExecutor

SESSIONS = "/api/v1/sessions"
RUNS = "/api/v1/runs"
DEMO_TOOLS = ["web_search", "calculator", "remember_fact"]


async def _create_session(
    client: AsyncClient, user: AuthedUser, tools: list[str] = DEMO_TOOLS
) -> str:
    response = await client.post(
        SESSIONS,
        json={
            "name": "Research Assistant",
            "system_prompt": "You are a careful research assistant.",
            "tools_enabled": tools,
        },
        headers=user.headers,
    )
    assert response.status_code == 201, response.text
    session_id: str = response.json()["id"]
    return session_id


async def _run(
    client: AsyncClient,
    user: AuthedUser,
    execute_run: RunExecutor,
    session_id: str,
    message: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Submit a message, execute it with the providers settings select; status and steps."""
    response = await client.post(
        f"{SESSIONS}/{session_id}/run", json={"message": message}, headers=user.headers
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    settings = get_settings()
    llm, embedder = create_llm_provider(settings), create_embedder(settings)
    try:
        await execute_run(run_id, llm, embedder)
    finally:
        await llm.aclose()
        await embedder.aclose()
    status = (await client.get(f"{RUNS}/{run_id}/status", headers=user.headers)).json()
    steps = (await client.get(f"{RUNS}/{run_id}/steps", headers=user.headers)).json()["items"]
    return status, steps


def _tool_results(steps: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {s["payload"]["name"]: s["payload"] for s in steps if s["step_type"] == "tool_result"}


def test_provider_defaults_to_fake_without_a_key_and_openai_with_one() -> None:
    # Passed explicitly: the test environment pins LLM_PROVIDER.
    assert Settings(llm_provider=None, openai_api_key=None).llm_provider == "fake"
    assert Settings(llm_provider=None, openai_api_key="sk-test").llm_provider == "openai"


async def test_flagship_scenario_plays_end_to_end(
    client: AsyncClient, authed_user: AuthedUser, execute_run: RunExecutor
) -> None:
    session_id = await _create_session(client, authed_user)

    status, steps = await _run(client, authed_user, execute_run, session_id, FLAGSHIP_PROMPT)

    assert status["status"] == "completed"
    assert [s["step_type"] for s in steps] == [
        "memory_retrieval",
        *["llm_call", "tool_call", "tool_result"] * 3,
        "llm_call",
        "final_answer",
    ]
    calls = [s["payload"] for s in steps if s["step_type"] == "tool_call"]
    assert [c["name"] for c in calls] == DEMO_TOOLS
    # The calculator works from the population the search actually returned.
    assert json.loads(calls[1]["arguments"]) == {"expression": "3655000 * 15 / 100"}
    results = _tool_results(steps)
    assert not any(r["is_error"] for r in results.values())
    assert results["calculator"]["result"] == "548250"
    assert "548,250" in status["final_answer"]
    assert "3,655,000" in status["final_answer"]
    assert status["tokens_used"] > 0

    memories = (await client.get("/api/v1/memory", headers=authed_user.headers)).json()
    [memory] = memories["items"]
    assert "548,250" in memory["content"]
    assert results["remember_fact"]["result"] == f"Remembered: {memory['content']}"


async def test_repeating_the_flagship_prompt_recalls_rather_than_duplicates(
    client: AsyncClient, authed_user: AuthedUser, execute_run: RunExecutor
) -> None:
    session_id = await _create_session(client, authed_user)
    await _run(client, authed_user, execute_run, session_id, FLAGSHIP_PROMPT)

    status, steps = await _run(client, authed_user, execute_run, session_id, FLAGSHIP_PROMPT)

    assert status["status"] == "completed"
    assert steps[0]["payload"]["memories"]
    assert _tool_results(steps)["remember_fact"]["result"].startswith("Already remembered:")
    assert "548,250" in status["final_answer"]
    memories = (await client.get("/api/v1/memory", headers=authed_user.headers)).json()
    assert memories["total"] == 1


async def test_flagship_without_a_needed_tool_answers_without_calling_it(
    client: AsyncClient, authed_user: AuthedUser, execute_run: RunExecutor
) -> None:
    session_id = await _create_session(client, authed_user, tools=["calculator"])

    status, steps = await _run(client, authed_user, execute_run, session_id, FLAGSHIP_PROMPT)

    assert status["status"] == "completed"
    assert [s["step_type"] for s in steps] == ["memory_retrieval", "llm_call", "final_answer"]
    assert "web_search" in status["final_answer"]


@pytest.mark.parametrize("message", ["Hello there!", "What's the weather like in London?"])
async def test_other_prompts_get_a_scripted_answer(
    client: AsyncClient, authed_user: AuthedUser, execute_run: RunExecutor, message: str
) -> None:
    session_id = await _create_session(client, authed_user)

    status, steps = await _run(client, authed_user, execute_run, session_id, message)

    assert status["status"] == "completed"
    assert [s["step_type"] for s in steps][-1] == "final_answer"
    assert FLAGSHIP_PROMPT in status["final_answer"]


async def test_demo_page_is_served_outside_the_api_prefix(client: AsyncClient) -> None:
    response = await client.get("/demo")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    assert "EventSource" in page
    assert "access_token" in page
    assert FLAGSHIP_PROMPT in page


def test_make_demo_script_plays_the_flagship_prompt() -> None:
    script = Path(__file__).parents[1] / "docker" / "demo.sh"

    assert f'PROMPT="{FLAGSHIP_PROMPT}"' in script.read_text()
