import logging
import re
import sys

import structlog
from structlog.types import Processor

from app.core.config import get_settings

_ACCESS_TOKEN_RE = re.compile(r"(?i)(access_token=)[^&\s]*")


def scrub_query_string(query: str) -> str:
    """Redact `access_token` values from a raw query string."""
    return _ACCESS_TOKEN_RE.sub(r"\1[REDACTED]", query)


def configure_logging() -> None:
    """Route structlog and stdlib logging through one JSON renderer.

    Context (request_id, user_id, session_id, run_id, step_type) is attached with
    `structlog.contextvars.bind_contextvars` and merged into every event.
    """
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(get_settings().log_level)

    # Uvicorn's own access log is replaced by the request middleware's scrubbed one.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
