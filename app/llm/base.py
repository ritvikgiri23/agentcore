from dataclasses import dataclass, field
from typing import Any, Protocol

# Chat messages and tool definitions use the OpenAI wire format.
type ChatMessage = dict[str, Any]
type ToolDefinition = dict[str, Any]


@dataclass(frozen=True)
class ToolCallRequest:
    id: str
    name: str
    # JSON-encoded, exactly as the model produced it; the dispatcher validates it.
    arguments: str


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class ChatResult:
    """A provider-independent chat completion."""

    model: str
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str | None = None
    usage: LLMUsage = field(default_factory=LLMUsage)
    latency_ms: float = 0.0

    def as_assistant_message(self) -> ChatMessage:
        """This result as the assistant message that goes back into the conversation."""
        message: ChatMessage = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return message


class LLMProvider(Protocol):
    async def chat(
        self, messages: list[ChatMessage], tools: list[ToolDefinition]
    ) -> ChatResult:
        """One chat completion; with tools, the model chooses whether to call them."""
        ...

    async def aclose(self) -> None: ...
