import asyncio
import time
from enum import StrEnum
from typing import Annotated

import pytest
from pydantic import Field

from app.core.config import get_settings
from app.tools import ToolContext, dispatch, get_tool_definitions, tool


def test_schema_is_generated_from_signature_and_docstring(isolated_registry: None) -> None:
    @tool
    def greet(
        name: Annotated[str, Field(description="Who to greet")],
        ctx: ToolContext,
        times: Annotated[int, Field(description="How many times")] = 1,
    ) -> str:
        """Greet someone by name."""
        return f"hi {name}" * times

    [definition] = get_tool_definitions(["greet"])

    assert definition == {
        "type": "function",
        "function": {
            "name": "greet",
            "description": "Greet someone by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Who to greet"},
                    "times": {"type": "integer", "description": "How many times", "default": 1},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    }


def test_schema_keeps_definitions_for_nested_types(isolated_registry: None) -> None:
    class Unit(StrEnum):
        CELSIUS = "celsius"
        FAHRENHEIT = "fahrenheit"

    @tool
    def get_weather(city: str, unit: Unit = Unit.CELSIUS) -> str:
        """Get the weather for a city."""
        return f"{city}: 20 {unit}"

    [definition] = get_tool_definitions(["get_weather"])
    parameters = definition["function"]["parameters"]

    assert parameters["properties"]["unit"]["$ref"] == "#/$defs/Unit"
    assert parameters["$defs"]["Unit"]["enum"] == ["celsius", "fahrenheit"]


async def test_newly_decorated_sync_tool_is_dispatchable(
    isolated_registry: None, tool_context: ToolContext
) -> None:
    @tool
    def whoami(ctx: ToolContext, shout: bool = False) -> str:
        """Report the calling user."""
        return ctx.user_id.upper() if shout else ctx.user_id

    outcome = await dispatch("whoami", '{"shout": true}', ["whoami"], tool_context)

    assert outcome.result == "TEST-USER"
    assert outcome.is_error is False
    assert outcome.timed_out is False
    assert outcome.latency_ms >= 0


async def test_async_tool_is_awaited(isolated_registry: None, tool_context: ToolContext) -> None:
    @tool
    async def echo(text: str) -> str:
        """Echo the text back."""
        return text

    outcome = await dispatch("echo", '{"text": "hello"}', ["echo"], tool_context)

    assert (outcome.result, outcome.is_error) == ("hello", False)


async def test_unknown_tool_lists_the_enabled_tools(tool_context: ToolContext) -> None:
    outcome = await dispatch("teleport", "{}", ["web_search", "calculator"], tool_context)

    assert outcome.is_error is True
    assert outcome.timed_out is False
    assert outcome.result == "Unknown tool 'teleport'. Available tools: calculator, web_search"


async def test_registered_but_disabled_tool_is_refused(
    isolated_registry: None, tool_context: ToolContext
) -> None:
    @tool
    def launch_missiles() -> str:
        """Not something this session may do."""
        raise AssertionError("a disabled tool must never run")

    outcome = await dispatch("launch_missiles", "{}", ["web_search"], tool_context)

    assert outcome.is_error is True
    assert outcome.result == (
        "Tool 'launch_missiles' is not enabled for this session. Available tools: web_search"
    )


async def test_refusal_says_so_when_no_tools_are_enabled(tool_context: ToolContext) -> None:
    outcome = await dispatch("teleport", "{}", [], tool_context)

    assert outcome.result == "Unknown tool 'teleport'. Available tools: none"


async def test_invalid_json_arguments_are_an_error(
    isolated_registry: None, tool_context: ToolContext
) -> None:
    @tool
    def echo(text: str) -> str:
        """Echo the text back."""
        return text

    outcome = await dispatch("echo", '{"text": ', ["echo"], tool_context)

    assert outcome.is_error is True
    assert outcome.result.startswith("Invalid JSON arguments for 'echo':")


async def test_arguments_must_be_a_json_object(
    isolated_registry: None, tool_context: ToolContext
) -> None:
    @tool
    def echo(text: str) -> str:
        """Echo the text back."""
        return text

    outcome = await dispatch("echo", '["hello"]', ["echo"], tool_context)

    assert outcome.is_error is True
    assert outcome.result == "Invalid arguments for 'echo': expected a JSON object"


async def test_schema_invalid_arguments_get_a_trimmed_validation_message(
    isolated_registry: None, tool_context: ToolContext
) -> None:
    @tool
    def repeat(text: str, times: int) -> str:
        """Repeat the text."""
        return text * times

    outcome = await dispatch(
        "repeat", '{"times": "lots", "colour": "red"}', ["repeat"], tool_context
    )

    assert outcome.is_error is True
    assert outcome.result == (
        "Invalid arguments for 'repeat': "
        "text: Field required; "
        "times: Input should be a valid integer, unable to parse string as an integer; "
        "colour: Extra inputs are not permitted"
    )


async def test_validation_message_is_capped_in_length(
    isolated_registry: None, tool_context: ToolContext
) -> None:
    @tool
    def echo(text: str) -> str:
        """Echo the text back."""
        return text

    junk = ", ".join(f'"extra_field_{i}": 1' for i in range(200))
    outcome = await dispatch("echo", "{" + junk + "}", ["echo"], tool_context)

    assert outcome.is_error is True
    assert len(outcome.result) <= 500
    assert outcome.result.endswith("…")


async def test_blank_arguments_mean_no_arguments(
    isolated_registry: None, tool_context: ToolContext
) -> None:
    @tool
    def ping() -> str:
        """Reply with pong."""
        return "pong"

    outcome = await dispatch("ping", "", ["ping"], tool_context)

    assert (outcome.result, outcome.is_error) == ("pong", False)


async def test_tool_exception_becomes_an_error_string(
    isolated_registry: None, tool_context: ToolContext
) -> None:
    @tool
    def explode() -> str:
        """Always fails."""
        raise RuntimeError("kaboom")

    outcome = await dispatch("explode", "{}", ["explode"], tool_context)

    assert outcome.is_error is True
    assert outcome.timed_out is False
    assert outcome.result == "Error in 'explode': RuntimeError: kaboom"


async def test_slow_async_tool_times_out(
    isolated_registry: None, tool_context: ToolContext
) -> None:
    @tool
    async def dawdle() -> str:
        """Takes far too long."""
        await asyncio.sleep(10)
        return "finally"

    outcome = await dispatch("dawdle", "{}", ["dawdle"], tool_context, timeout=0.05)

    assert outcome.is_error is True
    assert outcome.timed_out is True
    assert outcome.result == "Tool 'dawdle' timed out after 0.05 seconds"
    assert outcome.latency_ms < 1000


async def test_slow_sync_tool_times_out(
    isolated_registry: None, tool_context: ToolContext
) -> None:
    @tool
    def dawdle() -> str:
        """Blocks for too long."""
        time.sleep(1)
        return "finally"

    outcome = await dispatch("dawdle", "{}", ["dawdle"], tool_context, timeout=0.05)

    assert outcome.timed_out is True
    assert outcome.latency_ms < 500


async def test_timeout_raised_inside_a_tool_is_an_ordinary_error(
    isolated_registry: None, tool_context: ToolContext
) -> None:
    @tool
    async def flaky_upstream() -> str:
        """Calls an upstream that times out on its own."""
        raise TimeoutError("upstream took too long")

    outcome = await dispatch("flaky_upstream", "{}", ["flaky_upstream"], tool_context)

    assert outcome.is_error is True
    assert outcome.timed_out is False
    assert outcome.result == "Error in 'flaky_upstream': TimeoutError: upstream took too long"


async def test_timeout_defaults_to_the_configured_tool_timeout(
    isolated_registry: None, tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "tool_timeout_seconds", 0.05)

    @tool
    async def dawdle() -> str:
        """Takes far too long."""
        await asyncio.sleep(10)
        return "finally"

    outcome = await dispatch("dawdle", "{}", ["dawdle"], tool_context)

    assert outcome.timed_out is True
    assert outcome.result == "Tool 'dawdle' timed out after 0.05 seconds"


def test_tool_names_must_be_unique(isolated_registry: None) -> None:
    with pytest.raises(ValueError, match="already registered"):

        @tool
        def calculator(expression: str) -> str:
            """Shadows the built-in calculator."""
            return expression


def test_tools_need_a_docstring(isolated_registry: None) -> None:
    with pytest.raises(ValueError, match="docstring"):

        @tool
        def mystery() -> str:
            return "?"
