# 03: Tool registry and dispatcher

**What to build:** Tools are defined by writing a single decorated, type-hinted function with a docstring; its OpenAI function schema, registration, argument validation and availability follow automatically. A dispatcher runs any tool by name with a JSON argument string and always returns a structured outcome — it never raises. The first three tools work: `web_search`, `calculator` and `get_current_datetime`.

**Blocked by:** 01 (Walking skeleton)

**Status:** ready-for-agent

- [ ] Decorator builds a Pydantic model from the signature (per-parameter descriptions via annotated fields), uses the docstring as description, generates the OpenAI function schema and registers the tool in a module-level registry
- [ ] A parameter typed as `ToolContext` is injected by the dispatcher and excluded from the generated schema; `ToolContext` carries user id, run id, a DB session factory, the LLM provider and a token-usage accumulator (fields may be unused until later tickets)
- [ ] Sync tools run in a worker thread; async tools are awaited; every invocation wrapped in a 5-second timeout (configurable)
- [ ] Dispatcher takes name, JSON args string, the session's enabled tools and the context; returns result string, error flag, timed-out flag and latency
- [ ] Unknown tool and tool-not-enabled return an error outcome listing the available (enabled) tools; invalid JSON and schema-invalid arguments return a trimmed validation message; any tool exception returns an error string; timeouts return a timed-out outcome
- [ ] `web_search`: deterministic keyword fixture (Dubai population with a concrete number, weather, python, etc.) returning three snippets with title/url/snippet; generic fallback echoing the query
- [ ] `calculator`: whitelisted AST walker allowing numeric constants, `+ - * / // % **` and unary minus; rejects names, calls, attributes and everything else; caps exponent size and input length; division by zero → error string
- [ ] `get_current_datetime`: current UTC ISO timestamp
- [ ] Registry exposes "list tool names" and "definitions for a given set of names" for use by sessions and the planner
- [ ] Tests (dispatcher seam): schema matches signature/docstring and excludes ToolContext; a newly decorated test tool becomes dispatchable with no other change; calculator happy paths and rejection of `__import__`, names, calls, huge exponents, over-long input, 1/0; exception and timeout outcomes; unknown and disabled tool messages; web_search Dubai fixture
