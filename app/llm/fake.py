import copy
import itertools
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.llm.base import ChatMessage, ChatResult, LLMUsage, ToolCallRequest, ToolDefinition

FAKE_MODEL = "fake-llm"


@dataclass(frozen=True)
class FakeReply:
    """One scripted model response: plain content, tool calls, or both."""

    content: str | None = None
    # (tool name, arguments). Arguments given as a string are sent verbatim, so a test
    # can script malformed JSON.
    tool_calls: list[tuple[str, dict[str, Any] | str]] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=lambda: LLMUsage(10, 5))


def answer(content: str) -> FakeReply:
    return FakeReply(content=content)


def call_tool(name: str, arguments: dict[str, Any] | str) -> FakeReply:
    return FakeReply(tool_calls=[(name, arguments)])


@dataclass(frozen=True)
class RecordedRequest:
    messages: list[ChatMessage]
    tools: list[ToolDefinition]


class ScriptedLLM:
    """A deterministic LLM that plays back scripted replies in order.

    Every request is recorded so tests can inspect what the model was sent. Once the
    script runs out it repeats `fallback`, or raises if there is none.
    """

    def __init__(self, script: Iterable[FakeReply] = (), *, fallback: FakeReply | None = None):
        self._script = list(script)
        self._fallback = fallback
        self._call_ids = itertools.count(1)
        self.requests: list[RecordedRequest] = []

    async def chat(
        self, messages: list[ChatMessage], tools: list[ToolDefinition]
    ) -> ChatResult:
        self.requests.append(RecordedRequest(copy.deepcopy(messages), copy.deepcopy(tools)))
        if self._script:
            reply = self._script.pop(0)
        elif self._fallback is not None:
            reply = self._fallback
        else:
            raise RuntimeError(f"ScriptedLLM script exhausted after {len(self.requests) - 1} calls")
        tool_calls = [
            ToolCallRequest(
                id=f"call_{next(self._call_ids)}",
                name=name,
                arguments=arguments if isinstance(arguments, str) else json.dumps(arguments),
            )
            for name, arguments in reply.tool_calls
        ]
        return ChatResult(
            model=FAKE_MODEL,
            content=reply.content,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            usage=reply.usage,
        )

    async def aclose(self) -> None:
        return None
