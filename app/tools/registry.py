import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, get_type_hints

from pydantic import BaseModel, ConfigDict, create_model
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass
class TokenUsage:
    """Accumulates LLM token usage, including nested calls made by tools."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens


@dataclass
class ToolContext:
    """Server-side context injected into tools; never part of the LLM-facing schema."""

    user_id: str
    run_id: str
    session_factory: async_sessionmaker[AsyncSession] | None = None
    # The LLM provider; typed loosely until the planner abstraction exists.
    llm: Any = None
    usage: TokenUsage = field(default_factory=TokenUsage)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    fn: Callable[..., Any]
    args_model: type[BaseModel]
    context_param: str | None
    is_async: bool

    @property
    def definition(self) -> dict[str, Any]:
        """The OpenAI function-calling definition for this tool."""
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        parameters: dict[str, Any] = {
            "type": "object",
            "properties": schema.get("properties", {}),
            "required": schema.get("required", []),
            "additionalProperties": False,
        }
        # Enums and nested models are referenced from properties via "$ref".
        if "$defs" in schema:
            parameters["$defs"] = schema["$defs"]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }


TOOL_REGISTRY: dict[str, Tool] = {}


def tool[F: Callable[..., Any]](fn: F) -> F:
    """Register a type-hinted, documented function as a tool.

    Parameters become the tool's arguments; use `Annotated[T, Field(description=...)]` to
    describe them. A parameter typed as `ToolContext` is injected by the dispatcher instead.
    """
    name = fn.__name__
    if name in TOOL_REGISTRY:
        raise ValueError(f"Tool {name!r} is already registered")
    description = inspect.getdoc(fn)
    if not description:
        raise ValueError(f"Tool {name!r} needs a docstring to use as its description")

    hints = get_type_hints(fn, include_extras=True)
    context_param: str | None = None
    fields: dict[str, Any] = {}
    for param in inspect.signature(fn).parameters.values():
        if param.name not in hints:
            raise TypeError(f"Tool {name!r} parameter {param.name!r} needs a type hint")
        hint = hints[param.name]
        if hint is ToolContext:
            context_param = param.name
            continue
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[param.name] = (hint, default)

    args_model = create_model(
        f"{name}_args", __config__=ConfigDict(extra="forbid"), **fields
    )
    TOOL_REGISTRY[name] = Tool(
        name=name,
        description=description,
        fn=fn,
        args_model=args_model,
        context_param=context_param,
        is_async=inspect.iscoroutinefunction(fn),
    )
    return fn


def list_tool_names() -> list[str]:
    return sorted(TOOL_REGISTRY)


def get_tool_definitions(names: Iterable[str]) -> list[dict[str, Any]]:
    """OpenAI function definitions for the given registered tool names, in order."""
    return [TOOL_REGISTRY[name].definition for name in names]
