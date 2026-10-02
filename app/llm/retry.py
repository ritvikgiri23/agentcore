"""Retries transient provider failures, so a brief hiccup doesn't fail a whole run."""

import asyncio
import random

import openai
import structlog

from app.llm.base import ChatMessage, ChatResult, LLMProvider, ToolDefinition

logger = structlog.get_logger(__name__)

# Rate limits, 5xx and timeouts (a subclass of connection errors) may pass on their own;
# anything else, such as a rejected request, would fail the same way again.
_TRANSIENT = (openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError)
_MAX_DELAY_SECONDS = 8.0


class RetryingLLM:
    """Wraps a provider: up to `attempts` calls, with exponential backoff and full jitter."""

    def __init__(
        self,
        inner: LLMProvider,
        *,
        attempts: int,
        base_delay_seconds: float,
    ) -> None:
        self._inner = inner
        self._attempts = attempts
        self._base_delay = base_delay_seconds

    async def chat(
        self, messages: list[ChatMessage], tools: list[ToolDefinition]
    ) -> ChatResult:
        for attempt in range(1, self._attempts + 1):
            try:
                return await self._inner.chat(messages, tools)
            except _TRANSIENT as exc:
                if attempt == self._attempts:
                    raise
                delay = random.uniform(
                    0, min(_MAX_DELAY_SECONDS, self._base_delay * 2 ** (attempt - 1))
                )
                logger.warning(
                    "llm_call_retrying",
                    attempt=attempt,
                    error=type(exc).__name__,
                    delay_seconds=round(delay, 3),
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable: the last attempt returns or raises")

    async def aclose(self) -> None:
        await self._inner.aclose()
