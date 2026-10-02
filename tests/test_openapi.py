from typing import Any

import pytest
from httpx import AsyncClient

ERROR_REF = "#/components/schemas/ErrorResponse"


@pytest.fixture
async def spec(client: AsyncClient) -> dict[str, Any]:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    spec: dict[str, Any] = response.json()
    return spec


def _operations(spec: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (f"{method.upper()} {path}", op)
        for path, ops in spec["paths"].items()
        for method, op in ops.items()
    ]


async def test_every_operation_has_a_summary_and_a_named_operation_id(
    spec: dict[str, Any],
) -> None:
    operations = _operations(spec)
    for name, op in operations:
        assert op.get("summary"), name
        # Named after the route function, not FastAPI's path-derived default.
        assert "_api_v1_" not in op["operationId"], name
    operation_ids = [op["operationId"] for _, op in operations]
    assert len(set(operation_ids)) == len(operation_ids)


async def test_every_operation_declares_exactly_one_success_status(
    spec: dict[str, Any],
) -> None:
    for name, op in _operations(spec):
        successes = [code for code in op["responses"] if code.startswith("2")]
        assert len(successes) == 1, name


async def test_every_error_response_is_documented_as_the_json_error_envelope(
    spec: dict[str, Any],
) -> None:
    for name, op in _operations(spec):
        for code, response in op["responses"].items():
            if code.startswith("2"):
                continue
            assert response["content"] == {
                "application/json": {"schema": {"$ref": ERROR_REF}}
            }, f"{name} {code}"
    # FastAPI's own validation error shape is never what the API returns.
    assert "HTTPValidationError" not in spec["components"]["schemas"]


async def test_authenticated_operations_document_401(spec: dict[str, Any]) -> None:
    for name, op in _operations(spec):
        if op.get("security"):
            assert "401" in op["responses"], name


async def test_operations_with_path_ids_document_404(spec: dict[str, Any]) -> None:
    for name, op in _operations(spec):
        if "{" in name:
            assert "404" in op["responses"], name


async def test_stream_documents_its_event_stream_success_response(
    spec: dict[str, Any],
) -> None:
    op = spec["paths"]["/api/v1/runs/{run_id}/stream"]["get"]
    assert "text/event-stream" in op["responses"]["200"]["content"]
