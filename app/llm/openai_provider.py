import time
from typing import Any, cast

from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionMessageParam,
)

from app.llm.base import ChatMessage, ChatResult, LLMUsage, ToolCallRequest, ToolDefinition


class OpenAIProvider:
    def __init__(self, *, api_key: str, model: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def chat(
        self, messages: list[ChatMessage], tools: list[ToolDefinition]
    ) -> ChatResult:
        kwargs: dict[str, Any] = {}
        # The API rejects tool_choice without tools.
        if tools:
            kwargs = {"tools": tools, "tool_choice": "auto"}
        started = time.perf_counter()
        completion = await self._client.chat.completions.create(
            model=self._model,
            # Messages are built in the OpenAI wire format; the SDK's TypedDicts are stricter.
            messages=cast(list[ChatCompletionMessageParam], messages),
            **kwargs,
        )
        return normalise_completion(completion, latency_ms=(time.perf_counter() - started) * 1000)

    async def aclose(self) -> None:
        await self._client.close()


def normalise_completion(completion: ChatCompletion, *, latency_ms: float) -> ChatResult:
    choice = completion.choices[0]
    tool_calls = [
        ToolCallRequest(id=call.id, name=call.function.name, arguments=call.function.arguments)
        for call in choice.message.tool_calls or []
        # Only function tools are ever offered to the model.
        if isinstance(call, ChatCompletionMessageFunctionToolCall)
    ]
    usage = completion.usage
    return ChatResult(
        model=completion.model,
        content=choice.message.content,
        tool_calls=tool_calls,
        finish_reason=choice.finish_reason,
        usage=LLMUsage(
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        ),
        latency_ms=latency_ms,
    )


class OpenAIEmbedder:
    def __init__(self, *, api_key: str, model: str, dimensions: int) -> None:
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model
        self._dimensions = dimensions

    async def embed(self, text: str) -> list[float]:
        response = await self._client.embeddings.create(
            model=self._model, input=text, dimensions=self._dimensions
        )
        return response.data[0].embedding

    async def aclose(self) -> None:
        await self._client.close()
