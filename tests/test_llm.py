from openai.types.chat import ChatCompletion

from app.core.config import Settings
from app.llm import ChatResult, LLMUsage, ToolCallRequest, create_llm_provider
from app.llm.demo import DemoLLM
from app.llm.openai_provider import normalise_completion
from app.llm.retry import RetryingLLM


def _completion(message: dict[str, object], finish_reason: str) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-4o-mini-2024-07-18",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
        }
    )


def test_normalises_a_tool_calling_completion() -> None:
    completion = _completion(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "calculator", "arguments": '{"expression": "1+1"}'},
                }
            ],
        },
        "tool_calls",
    )

    result = normalise_completion(completion, latency_ms=12.5)

    assert result == ChatResult(
        model="gpt-4o-mini-2024-07-18",
        content=None,
        tool_calls=[
            ToolCallRequest(id="call_abc", name="calculator", arguments='{"expression": "1+1"}')
        ],
        finish_reason="tool_calls",
        usage=LLMUsage(prompt_tokens=120, completion_tokens=30),
        latency_ms=12.5,
    )


def test_normalises_a_plain_answer() -> None:
    completion = _completion({"role": "assistant", "content": "Hello"}, "stop")

    result = normalise_completion(completion, latency_ms=1.0)

    assert result.content == "Hello"
    assert result.tool_calls == []
    assert result.finish_reason == "stop"
    assert result.usage.total_tokens == 150


async def test_provider_follows_settings() -> None:
    fake = create_llm_provider(Settings(llm_provider="fake"))
    real = create_llm_provider(Settings(llm_provider="openai", openai_api_key="sk-test"))
    try:
        assert isinstance(fake, DemoLLM)
        assert isinstance(real, RetryingLLM)
    finally:
        await fake.aclose()
        await real.aclose()
