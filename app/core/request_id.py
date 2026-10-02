import re
import time
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import scrub_query_string

REQUEST_ID_HEADER = "X-Request-ID"

# Client-supplied ids are echoed and logged, so only accept short, printable tokens.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

access_logger = structlog.get_logger("app.access")


def get_request_id(request: Request) -> str | None:
    request_id: str | None = request.scope.get("state", {}).get("request_id")
    return request_id


class RequestContextMiddleware:
    """Assigns a request id, echoes it on the response, binds it into the log
    context and writes one access-log line per request (with tokens scrubbed)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Request(scope).headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        status_code = 500
        start = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            access_logger.info(
                "request",
                method=scope["method"],
                path=scope["path"],
                query=scrub_query_string(scope.get("query_string", b"").decode("latin-1")),
                status_code=status_code,
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
            )
