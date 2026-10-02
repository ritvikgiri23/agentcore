from fastapi import FastAPI

from app import demo
from app.api.v1.router import api_router
from app.core import openapi
from app.core.errors import ErrorResponse, register_exception_handlers
from app.core.logging import configure_logging
from app.core.request_id import RequestContextMiddleware


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="AgentCore",
        version="0.1.0",
        description=(
            "Submit tasks to tool-using AI agents, watch them reason step by step over "
            "Server-Sent Events, and let them remember facts about you.\n\n"
            "Authenticate with `POST /api/v1/auth/register` or `POST /api/v1/auth/token`, then "
            "send `Authorization: Bearer <access_token>`. Every error uses the envelope "
            "`{\"error\": {\"code\", \"message\", \"details\", \"request_id\"}}`."
        ),
        openapi_tags=openapi.TAGS,
        generate_unique_id_function=openapi.operation_id,
        responses={500: {"model": ErrorResponse, "description": "Unexpected server error"}},
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router)
    app.include_router(demo.router)
    openapi.install(app)
    return app


app = create_app()
