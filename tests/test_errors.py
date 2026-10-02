from fastapi import FastAPI, HTTPException
from httpx import AsyncClient

from app.core.errors import ConflictError


async def test_unknown_route_returns_not_found_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["message"] == "Not Found"
    assert error["details"] is None
    assert error["request_id"] == response.headers["X-Request-ID"]


async def test_unhandled_exception_returns_generic_internal_error(
    app: FastAPI, client: AsyncClient
) -> None:
    @app.get("/api/v1/_boom")
    async def boom() -> None:
        raise RuntimeError("secret database password is hunter2")

    response = await client.get("/api/v1/_boom")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "An unexpected error occurred",
            "details": None,
            "request_id": response.headers["X-Request-ID"],
        }
    }
    assert "hunter2" not in response.text
    assert "RuntimeError" not in response.text


async def test_application_error_renders_code_message_and_details(
    app: FastAPI, client: AsyncClient
) -> None:
    @app.get("/api/v1/_conflict")
    async def conflict() -> None:
        raise ConflictError("Email already registered", details={"field": "email"})

    response = await client.get("/api/v1/_conflict")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "conflict"
    assert error["message"] == "Email already registered"
    assert error["details"] == {"field": "email"}
    assert error["request_id"] == response.headers["X-Request-ID"]


async def test_request_validation_error_lists_invalid_fields(
    app: FastAPI, client: AsyncClient
) -> None:
    @app.get("/api/v1/_items")
    async def items(limit: int) -> None:
        return None

    response = await client.get("/api/v1/_items", params={"limit": "lots"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["message"] == "Request validation failed"
    assert [d["loc"] for d in error["details"]] == [["query", "limit"]]
    assert error["request_id"] == response.headers["X-Request-ID"]


async def test_method_not_allowed_uses_envelope(client: AsyncClient) -> None:
    response = await client.post("/api/v1/health")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


async def test_http_exception_with_structured_detail_puts_it_in_details(
    app: FastAPI, client: AsyncClient
) -> None:
    @app.get("/api/v1/_teapot")
    async def teapot() -> None:
        raise HTTPException(status_code=400, detail={"reason": "short and stout"})

    response = await client.get("/api/v1/_teapot")

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "bad_request"
    assert error["message"] == "Bad Request"
    assert error["details"] == {"reason": "short and stout"}
