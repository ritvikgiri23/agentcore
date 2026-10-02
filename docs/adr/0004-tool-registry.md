# 0004: Tools are decorated, type-hinted functions; the registry derives everything else

Status: accepted

## Context

Each tool needs:
- an OpenAI function schema;
- argument validation (the LLM sends arbitrary JSON);
- a description;
- registration;
- a per-session allowlist;
- a timeout;
- access to server-side context (who the user is, a DB session, the LLM) that the
  model must never control.

Hand-written schemas drift from the code. Passing the user id as a normal argument lets
the model forge it.

## Decision

A `@tool` decorator (`app/tools/registry.py`) builds the tool from the function:

- **Name:** the function's name.
- **Description:** the docstring (required).
- **Arguments:** a Pydantic model created from the parameters' type hints, with
  `extra="forbid"`. Per-argument descriptions and constraints come from
  `Annotated[T, Field(...)]`.
- **Schema:** the model's JSON schema, reshaped into an OpenAI function definition
  (`additionalProperties: false`, `$defs` kept for enums and nested models).
- **Context:** a parameter typed `ToolContext` is excluded from the schema and
  injected by the dispatcher. It carries the user id, run id, session factory, LLM,
  embedder and a token-usage accumulator.
- **Sync or async:** detected automatically. Sync tools run in a thread; async tools
  are awaited.

`dispatch(name, args_json, enabled_tools, ctx)` (`app/tools/dispatcher.py`) is the only
way a tool runs. It **never raises** for anything the model or the tool got wrong.
Each of these becomes a `ToolOutcome(result, is_error, timed_out, latency_ms)` that goes
back to the model:

- an unknown tool;
- a tool not enabled for the session (both list the available tools);
- invalid JSON;
- a non-object argument;
- arguments that fail validation;
- an exception inside the tool;
- a timeout.

Enablement is checked here as well as by which schemas are sent, so a prompt-injected
call can't reach a disabled tool.

Session creation validates `tools_enabled` against the registry, with a 422 listing
the unknown and available names.

## Consequences

Adding a tool is one function in `app/tools/` plus an import in `app/tools/__init__.py`.
Schema, validation, registration and session availability follow from it:

```python
@tool
def get_weather(city: Annotated[str, Field(description="City name")]) -> str:
    """Current weather for a city."""
    ...
```

- The schema can't drift from the signature. Tests assert the generated schemas and
  that `ToolContext` never appears in them.
- Tools are trivially unit-testable through `dispatch`.
- A timed-out sync tool's thread can't be killed; it is abandoned and finishes in the
  background. Tools that may block for long should be async.
- The registry is module-level and populated at import time. Name clashes fail loudly
  at startup.
