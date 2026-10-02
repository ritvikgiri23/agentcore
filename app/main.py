from fastapi import FastAPI

from app import demo
from app.api.v1.router import api_router
from app.core.errors import ErrorResponse, register_exception_handlers
from app.core.logging import configure_logging
from app.core.request_id import RequestContextMiddleware


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="AgentCore",
        version="0.1.0",
        responses={500: {"model": ErrorResponse, "description": "Unexpected server error"}},
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router)
    app.include_router(demo.router)
    return app


app = create_app()
