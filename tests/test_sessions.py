from typing import Any

import pytest
from httpx import AsyncClient, Response

from app.tools import list_tool_names
from tests.conftest import AgentSessionFactory, AuthedUser, UserFactory

SESSIONS = "/api/v1/sessions"


async def _create(client: AsyncClient, user: AuthedUser, **overrides: Any) -> Response:
    body = {
        "name": "Research Assistant",
        "system_prompt": "You are a careful research assistant.",
        "tools_enabled": ["web_search", "calculator"],
    } | overrides
    return await client.post(SESSIONS, json=body, headers=user.headers)


async def test_create_session(client: AsyncClient, authed_user: AuthedUser) -> None:
    response = await _create(client, authed_user)

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Research Assistant"
    assert body["system_prompt"] == "You are a careful research assistant."
    assert body["tools_enabled"] == ["web_search", "calculator"]
    assert body["id"]
    assert body["created_at"]


async def test_create_session_rejects_unknown_tools(
    client: AsyncClient, authed_user: AuthedUser
) -> None:
    response = await _create(
        client, authed_user, tools_enabled=["web_search", "teleport", "mind_reader", "teleport"]
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "unknown_tool"
    assert error["message"] == "Unknown tools: teleport, mind_reader"
    assert error["details"] == {
        "unknown": ["teleport", "mind_reader"],
        "available": list_tool_names(),
    }


async def test_create_session_deduplicates_tools(
    client: AsyncClient, authed_user: AuthedUser
) -> None:
    response = await _create(
        client, authed_user, tools_enabled=["calculator", "web_search", "calculator"]
    )

    assert response.status_code == 201
    assert response.json()["tools_enabled"] == ["calculator", "web_search"]


async def test_create_session_allows_no_tools(
    client: AsyncClient, authed_user: AuthedUser
) -> None:
    response = await _create(client, authed_user, tools_enabled=[])

    assert response.status_code == 201
    assert response.json()["tools_enabled"] == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("name", ""), ("name", "n" * 101), ("system_prompt", "p" * 4001)],
)
async def test_create_session_enforces_size_limits(
    client: AsyncClient, authed_user: AuthedUser, field: str, value: str
) -> None:
    response = await _create(client, authed_user, **{field: value})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"][0]["loc"] == ["body", field]


async def test_create_session_accepts_maximum_sizes(
    client: AsyncClient, authed_user: AuthedUser
) -> None:
    response = await _create(client, authed_user, name="n" * 100, system_prompt="p" * 4000)

    assert response.status_code == 201


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", SESSIONS),
        ("GET", SESSIONS),
        ("GET", f"{SESSIONS}/some-id"),
        ("DELETE", f"{SESSIONS}/some-id"),
    ],
)
async def test_sessions_require_authentication(
    client: AsyncClient, method: str, path: str
) -> None:
    body = {"name": "x", "system_prompt": "y", "tools_enabled": []}
    response = await client.request(method, path, json=body if method == "POST" else None)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "   "),
        ("name", "nul\x00byte"),
        ("system_prompt", "nul\x00byte"),
        ("tools_enabled", ["t" * 65]),
        ("tools_enabled", [f"tool_{i}" for i in range(51)]),
    ],
)
async def test_create_session_rejects_unusable_input(
    client: AsyncClient, authed_user: AuthedUser, field: str, value: object
) -> None:
    response = await _create(client, authed_user, **{field: value})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_create_session_trims_the_name(
    client: AsyncClient, authed_user: AuthedUser
) -> None:
    response = await _create(client, authed_user, name="  Research Assistant \n")

    assert response.json()["name"] == "Research Assistant"


async def test_list_sessions_paginates_newest_first(
    client: AsyncClient, authed_user: AuthedUser, agent_session_factory: AgentSessionFactory
) -> None:
    for name in ["first", "second", "third"]:
        await agent_session_factory(authed_user, name=name)

    page_one = await client.get(SESSIONS, params={"limit": 2}, headers=authed_user.headers)
    page_two = await client.get(
        SESSIONS, params={"limit": 2, "offset": 2}, headers=authed_user.headers
    )

    assert page_one.status_code == 200
    body = page_one.json()
    assert [s["name"] for s in body["items"]] == ["third", "second"]
    assert (body["total"], body["limit"], body["offset"]) == (3, 2, 0)
    assert [s["name"] for s in page_two.json()["items"]] == ["first"]


async def test_list_sessions_defaults_to_fifty(
    client: AsyncClient, authed_user: AuthedUser
) -> None:
    response = await client.get(SESSIONS, headers=authed_user.headers)

    assert response.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


@pytest.mark.parametrize(
    "params",
    [{"limit": 201}, {"limit": 0}, {"offset": -1}, {"offset": 99999999999999999999}],
)
async def test_list_sessions_rejects_out_of_range_paging(
    client: AsyncClient, authed_user: AuthedUser, params: dict[str, int]
) -> None:
    response = await client.get(SESSIONS, params=params, headers=authed_user.headers)

    assert response.status_code == 422


async def test_list_sessions_shows_only_the_callers_sessions(
    client: AsyncClient,
    authed_user: AuthedUser,
    user_factory: UserFactory,
    agent_session_factory: AgentSessionFactory,
) -> None:
    other = await user_factory()
    await agent_session_factory(other, name="not yours")
    mine = await agent_session_factory(authed_user, name="mine")

    response = await client.get(SESSIONS, headers=authed_user.headers)

    body = response.json()
    assert [s["id"] for s in body["items"]] == [mine.id]
    assert body["total"] == 1


async def test_get_session_detail(
    client: AsyncClient, authed_user: AuthedUser, agent_session_factory: AgentSessionFactory
) -> None:
    session = await agent_session_factory(authed_user, tools_enabled=["calculator"])

    response = await client.get(f"{SESSIONS}/{session.id}", headers=authed_user.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == session.id
    assert body["name"] == "Research Assistant"
    assert body["tools_enabled"] == ["calculator"]
    assert body["recent_runs"] == []


async def test_delete_session(
    client: AsyncClient, authed_user: AuthedUser, agent_session_factory: AgentSessionFactory
) -> None:
    session = await agent_session_factory(authed_user)

    deleted = await client.delete(f"{SESSIONS}/{session.id}", headers=authed_user.headers)
    fetched = await client.get(f"{SESSIONS}/{session.id}", headers=authed_user.headers)

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert fetched.status_code == 404


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_another_users_session_is_not_found(
    client: AsyncClient,
    authed_user: AuthedUser,
    user_factory: UserFactory,
    agent_session_factory: AgentSessionFactory,
    method: str,
) -> None:
    owner = await user_factory()
    session = await agent_session_factory(owner)

    url = f"{SESSIONS}/{session.id}"
    response = await client.request(method, url, headers=authed_user.headers)
    missing = await client.request(method, f"{SESSIONS}/no-such-id", headers=authed_user.headers)

    assert response.status_code == 404
    # Indistinguishable from a session that does not exist.
    assert response.json()["error"] | {"request_id": None} == missing.json()["error"] | {
        "request_id": None
    }
    still_there = await client.get(f"{SESSIONS}/{session.id}", headers=owner.headers)
    assert still_there.status_code == 200


@pytest.mark.parametrize("session_id", ["not-a-uuid", "%00", "a%00b", "x" * 2000])
async def test_malformed_session_ids_are_not_found(
    client: AsyncClient, authed_user: AuthedUser, session_id: str
) -> None:
    response = await client.get(f"{SESSIONS}/{session_id}", headers=authed_user.headers)

    assert response.status_code == 404
