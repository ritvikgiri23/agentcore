from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_embedder
from tests.conftest import (
    AgentRunFactory,
    AgentSessionFactory,
    AuthedUser,
    FailingEmbedder,
    MemoryFactory,
    UserFactory,
)

MEMORY = "/api/v1/memory"
MISSING_ID = "00000000-0000-4000-8000-000000000000"


async def test_list_returns_your_memories_newest_first(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    memory_factory: MemoryFactory,
) -> None:
    run = await agent_run_factory(await agent_session_factory(authed_user))
    older = await memory_factory(authed_user, "The user lives in Dubai", source_run_id=run.id)
    newer = await memory_factory(authed_user, "The user prefers metric units")

    response = await client.get(MEMORY, headers=authed_user.headers)

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": newer.id,
                "content": "The user prefers metric units",
                "source_run_id": None,
                "created_at": newer.created_at.isoformat().replace("+00:00", "Z"),
            },
            {
                "id": older.id,
                "content": "The user lives in Dubai",
                "source_run_id": run.id,
                "created_at": older.created_at.isoformat().replace("+00:00", "Z"),
            },
        ],
        "total": 2,
        "limit": 50,
        "offset": 0,
    }


async def test_list_is_paginated(
    client: AsyncClient, authed_user: AuthedUser, memory_factory: MemoryFactory
) -> None:
    for n in range(5):
        await memory_factory(authed_user, f"Fact {n}")

    response = await client.get(
        MEMORY, params={"limit": 2, "offset": 1}, headers=authed_user.headers
    )

    page = response.json()
    assert (page["total"], page["limit"], page["offset"]) == (5, 2, 1)
    assert [m["content"] for m in page["items"]] == ["Fact 3", "Fact 2"]


async def test_search_returns_the_top_five_nearest_first(
    client: AsyncClient, authed_user: AuthedUser, memory_factory: MemoryFactory
) -> None:
    contents = [
        "unrelated note",
        "dubai weather is hot today",
        "dubai population",
        "dubai population is large",
        "dubai population is large and growing",
        "dubai population is large and growing fast",
    ]
    memories = {c: await memory_factory(authed_user, c) for c in contents}

    response = await client.get(
        f"{MEMORY}/search", params={"q": "Dubai population"}, headers=authed_user.headers
    )

    assert response.status_code == 200
    results = response.json()
    assert [r["content"] for r in results] == [
        "dubai population",
        "dubai population is large",
        "dubai population is large and growing",
        "dubai population is large and growing fast",
        "dubai weather is hot today",
    ]
    exact = results[0]
    assert exact["id"] == memories["dubai population"].id
    assert abs(exact["distance"]) < 1e-6
    assert set(exact) == {"id", "content", "distance", "source_run_id", "created_at"}
    distances = [r["distance"] for r in results]
    assert distances == sorted(distances)


async def test_search_requires_a_query(client: AsyncClient, authed_user: AuthedUser) -> None:
    response = await client.get(f"{MEMORY}/search", params={"q": ""}, headers=authed_user.headers)

    assert response.status_code == 422


async def test_search_reports_an_embedding_outage(
    app: FastAPI, client: AsyncClient, authed_user: AuthedUser
) -> None:
    app.dependency_overrides[get_embedder] = FailingEmbedder

    response = await client.get(
        f"{MEMORY}/search", params={"q": "Dubai"}, headers=authed_user.headers
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


async def test_delete_removes_the_memory(
    client: AsyncClient, authed_user: AuthedUser, memory_factory: MemoryFactory
) -> None:
    memory = await memory_factory(authed_user, "The user lives in Dubai")
    kept = await memory_factory(authed_user, "The user prefers metric units")

    response = await client.delete(f"{MEMORY}/{memory.id}", headers=authed_user.headers)

    assert response.status_code == 204
    listed = (await client.get(MEMORY, headers=authed_user.headers)).json()
    assert [m["id"] for m in listed["items"]] == [kept.id]
    again = await client.delete(f"{MEMORY}/{memory.id}", headers=authed_user.headers)
    assert again.status_code == 404


async def test_delete_unknown_or_malformed_id_is_404(
    client: AsyncClient, authed_user: AuthedUser
) -> None:
    for memory_id in (MISSING_ID, "not-a-uuid"):
        response = await client.delete(f"{MEMORY}/{memory_id}", headers=authed_user.headers)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


async def test_memories_survive_deleting_the_session_that_created_them(
    client: AsyncClient,
    authed_user: AuthedUser,
    agent_session_factory: AgentSessionFactory,
    agent_run_factory: AgentRunFactory,
    memory_factory: MemoryFactory,
    db_session: AsyncSession,
) -> None:
    agent_session = await agent_session_factory(authed_user)
    run = await agent_run_factory(agent_session)
    memory = await memory_factory(authed_user, "The user lives in Dubai", source_run_id=run.id)

    deleted = await client.delete(
        f"/api/v1/sessions/{agent_session.id}", headers=authed_user.headers
    )

    assert deleted.status_code == 204
    # The database nulled the foreign key; the test's shared session still caches the old value.
    db_session.expire_all()
    [item] = (await client.get(MEMORY, headers=authed_user.headers)).json()["items"]
    assert (item["id"], item["source_run_id"]) == (memory.id, None)


async def test_other_users_memories_are_invisible_on_every_route(
    client: AsyncClient,
    authed_user: AuthedUser,
    user_factory: UserFactory,
    memory_factory: MemoryFactory,
) -> None:
    other = await user_factory()
    theirs = await memory_factory(other, "The other user lives in Dubai")
    mine = await memory_factory(authed_user, "I live in Abu Dhabi")

    listed = await client.get(MEMORY, headers=authed_user.headers)
    searched = await client.get(
        f"{MEMORY}/search",
        params={"q": "The other user lives in Dubai"},
        headers=authed_user.headers,
    )
    deleted = await client.delete(f"{MEMORY}/{theirs.id}", headers=authed_user.headers)

    assert [m["id"] for m in listed.json()["items"]] == [mine.id]
    assert listed.json()["total"] == 1
    assert [r["id"] for r in searched.json()] == [mine.id]
    assert deleted.status_code == 404
    # Still there for its owner.
    owner_view = await client.get(MEMORY, headers=other.headers)
    assert [m["id"] for m in owner_view.json()["items"]] == [theirs.id]


async def test_memory_routes_require_authentication(client: AsyncClient) -> None:
    for method, url in (
        ("GET", MEMORY),
        ("GET", f"{MEMORY}/search?q=x"),
        ("DELETE", f"{MEMORY}/{MISSING_ID}"),
    ):
        response = await client.request(method, url)
        assert response.status_code == 401, (method, url)
