import logging

import pytest
from httpx import AsyncClient


async def test_supplied_request_id_is_echoed(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"X-Request-ID": "abc-123"})

    assert response.headers["X-Request-ID"] == "abc-123"


async def test_request_id_is_generated_when_missing(client: AsyncClient) -> None:
    first = await client.get("/api/v1/health")
    second = await client.get("/api/v1/health")

    assert first.headers["X-Request-ID"]
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]


async def test_unsafe_request_id_is_replaced(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"X-Request-ID": "bad id\twith spaces"})

    assert response.headers["X-Request-ID"] != "bad id\twith spaces"
    assert response.headers["X-Request-ID"]


async def test_error_envelope_carries_supplied_request_id(client: AsyncClient) -> None:
    response = await client.get("/api/v1/nope", headers={"X-Request-ID": "trace-42"})

    assert response.json()["error"]["request_id"] == "trace-42"
    assert response.headers["X-Request-ID"] == "trace-42"


async def test_request_log_scrubs_access_token_and_carries_request_id(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.access"):
        await client.get(
            "/api/v1/health?access_token=super-secret-jwt&x=1",
            headers={"X-Request-ID": "log-1"},
        )

    access = [r.msg for r in caplog.records if r.name == "app.access"]
    assert len(access) == 1
    entry = access[0]
    assert isinstance(entry, dict)
    assert entry["request_id"] == "log-1"
    assert entry["status_code"] == 200
    assert "super-secret-jwt" not in str(entry)
    assert entry["query"] == "access_token=[REDACTED]&x=1"
