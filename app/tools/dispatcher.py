import asyncio
import json
import time
from collections.abc import Collection
from dataclasses import dataclass

from pydantic import ValidationError

from app.core.config import get_settings
from app.tools.registry import TOOL_REGISTRY, ToolContext

_MAX_MESSAGE_CHARS = 500


@dataclass(frozen=True)
class ToolOutcome:
    result: str
    is_error: bool
    timed_out: bool
    latency_ms: float


async def dispatch(
    name: str,
    args_json: str,
    enabled_tools: Collection[str],
    ctx: ToolContext,
    *,
    timeout: float | None = None,
) -> ToolOutcome:
    """Run a tool by name with JSON-encoded arguments.

    Never raises for anything the tool or the LLM got wrong: unknown or disabled tools,
    bad arguments, tool exceptions and timeouts all come back as error outcomes.
    Cancellation of the calling task still propagates.
    """
    started = time.perf_counter()
    timeout = get_settings().tool_timeout_seconds if timeout is None else timeout

    def outcome(result: str, *, is_error: bool = False, timed_out: bool = False) -> ToolOutcome:
        return ToolOutcome(
            result=result,
            is_error=is_error,
            timed_out=timed_out,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    def error(message: str) -> ToolOutcome:
        return outcome(message, is_error=True)

    available = ", ".join(sorted(set(enabled_tools))) or "none"
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        return error(f"Unknown tool {name!r}. Available tools: {available}")
    # Enforced here too, not only by which schemas are sent, so prompt injection
    # cannot reach a tool the session did not enable.
    if name not in enabled_tools:
        return error(
            f"Tool {name!r} is not enabled for this session. Available tools: {available}"
        )

    try:
        raw_args = json.loads(args_json) if args_json.strip() else {}
    except json.JSONDecodeError as exc:
        return error(_trim(f"Invalid JSON arguments for {name!r}: {exc}"))
    if not isinstance(raw_args, dict):
        return error(f"Invalid arguments for {name!r}: expected a JSON object")
    try:
        args = tool.args_model.model_validate(raw_args)
    except ValidationError as exc:
        return error(_trim(f"Invalid arguments for {name!r}: {_format_validation_errors(exc)}"))

    kwargs = dict(args)
    if tool.context_param is not None:
        kwargs[tool.context_param] = ctx
    # A sync tool's thread cannot be killed: on timeout it is abandoned and left to finish.
    # An async tool must not block the event loop, or the timeout cannot fire.
    call = tool.fn(**kwargs) if tool.is_async else asyncio.to_thread(tool.fn, **kwargs)
    deadline = asyncio.timeout(timeout)
    try:
        async with deadline:
            result = await call
    except Exception as exc:
        if isinstance(exc, TimeoutError) and deadline.expired():
            return outcome(
                f"Tool {name!r} timed out after {timeout:g} seconds",
                is_error=True,
                timed_out=True,
            )
        return error(_trim(f"Error in {name!r}: {type(exc).__name__}: {exc}"))
    return outcome(str(result))


def _format_validation_errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or '(arguments)'}: {err['msg']}"
        for err in exc.errors()
    )


def _trim(message: str) -> str:
    if len(message) <= _MAX_MESSAGE_CHARS:
        return message
    return message[: _MAX_MESSAGE_CHARS - 1] + "…"
