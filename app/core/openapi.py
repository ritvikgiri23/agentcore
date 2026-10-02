"""OpenAPI document tweaks, so the schema describes the responses the API really sends."""

from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

_SCHEMAS_REF = "#/components/schemas/"
_ERROR_SCHEMA = {"$ref": f"{_SCHEMAS_REF}ErrorResponse"}
# FastAPI's own validation error models, which the API never sends.
_VALIDATION_SCHEMAS = ("HTTPValidationError", "ValidationError")

TAGS: list[dict[str, Any]] = [
    {"name": "health", "description": "Liveness."},
    {
        "name": "auth",
        "description": "Register and log in. Both return a short-lived bearer access token.",
    },
    {
        "name": "sessions",
        "description": "Agent personas: a system prompt plus the tools the agent may use. "
        "Submitting a message to a session starts a run.",
    },
    {
        "name": "runs",
        "description": "Poll, trace, stream and cancel runs.",
    },
    {
        "name": "memory",
        "description": "The long-term facts the agent has remembered about you.",
    },
    {"name": "demo", "description": "A browser page that plays the flagship scenario."},
]


def operation_id(route: APIRoute) -> str:
    """Operation ids are the route functions' names: stable, and readable in clients."""
    return route.name


def install(app: FastAPI) -> None:
    """Wrap `app.openapi` so FastAPI's cached document is post-processed once."""
    build = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            _document_errors_as_envelope(build())
        return build()

    app.openapi = openapi  # type: ignore[method-assign]


def _document_errors_as_envelope(schema: dict[str, Any]) -> None:
    """Every error response is the JSON envelope, whatever the route's success media type.

    FastAPI documents its own validation error shape for 422s a route didn't declare, and
    gives error models the route's response class (an event stream, an HTML page). Neither
    is what the exception handlers send.
    """
    for operations in schema.get("paths", {}).values():
        for operation in operations.values():
            for code, response in operation.get("responses", {}).items():
                if code.startswith("2"):
                    continue
                if _references(response, f"{_SCHEMAS_REF}{_VALIDATION_SCHEMAS[0]}"):
                    response["description"] = "Request validation failed"
                response["content"] = {"application/json": {"schema": _ERROR_SCHEMA}}
    components = schema.get("components", {}).get("schemas", {})
    for name in _VALIDATION_SCHEMAS:
        components.pop(name, None)


def _references(response: dict[str, Any], ref: str) -> bool:
    return any(
        media.get("schema", {}).get("$ref") == ref
        for media in response.get("content", {}).values()
    )
