import copy
import hashlib
import itertools
import json
import math
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from app.llm.base import (
    EMBEDDING_DIMENSIONS,
    ChatMessage,
    ChatResult,
    LLMUsage,
    ToolCallRequest,
    ToolDefinition,
)

FAKE_MODEL = "fake-llm"
_WORD = re.compile(r"\w+")


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
        return to_chat_result(reply, self._call_ids)

    async def aclose(self) -> None:
        return None


def to_chat_result(reply: FakeReply, call_ids: Iterator[int]) -> ChatResult:
    """The completion a fake model returns for a reply; tool call ids come from `call_ids`."""
    tool_calls = [
        ToolCallRequest(
            id=f"call_{next(call_ids)}",
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


class HashEmbedder:
    """A deterministic bag-of-words embedder: no model, no network.

    Each word is hashed into one dimension, so identical text maps to identical vectors
    and texts sharing more words are closer — enough for reproducible similarity ranking.
    Texts with no words in common are orthogonal (cosine distance 1), barring collisions.
    """

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        self._dimensions = dimensions

    async def embed(self, text: str) -> list[float]:
        # Text with no words still needs a nonzero vector: cosine distance to zero is undefined.
        tokens = _WORD.findall(text.lower()) or [text]
        vector = [0.0] * self._dimensions
        for token in tokens:
            # sha256, not hash(): Python's string hash is salted per process.
            digest = hashlib.sha256(token.encode()).digest()
            vector[int.from_bytes(digest[:8], "big") % self._dimensions] += 1.0
        norm = math.sqrt(sum(x * x for x in vector))
        return [x / norm for x in vector]

    async def aclose(self) -> None:
        return None
