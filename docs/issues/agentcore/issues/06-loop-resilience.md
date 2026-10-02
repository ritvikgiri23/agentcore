# 06: Loop resilience and summarise_text

**What to build:** The agent loop survives everything an LLM or a tool can throw at it. Hallucinated or disabled tool names, malformed or invalid arguments, tool exceptions and slow tools all become visible steps and the agent keeps going; multiple tool calls in one response run concurrently; an unexpected crash ends the run as failed with an explanation instead of leaving it stuck. The `summarise_text` tool lets the agent condense text via a nested LLM call whose tokens count toward the run.

**Blocked by:** 05 (Tracer run)

**Status:** ready-for-agent

- [ ] Unknown or disabled tool, invalid JSON and schema-invalid arguments produce a `tool_result` with the error flag; every tool-call id still receives a tool message; the loop continues
- [ ] Multiple tool calls in one LLM response: `tool_call` steps recorded in order before execution, tools run concurrently, `tool_result`/`tool_timeout` steps recorded, tool messages appended in the original call order
- [ ] Tool timeout records a `tool_timeout` step (tool call id, name, timeout seconds) and the loop continues
- [ ] Unhandled exception in the runner records an `error` step (type and message only; traceback in logs) and marks the run failed; `done` published with status failed
- [ ] Tool enablement enforced at dispatch time, independent of which schemas were sent
- [ ] `summarise_text` (async): calls the planner with no tools and a word-limit prompt, hard-truncates to `max_words`, adds its usage to the run's tokens used via the tool context
- [ ] Tests (runner seam): hallucinated tool, disabled tool, malformed JSON, invalid args, tool exception, timeout, parallel calls preserve order and run concurrently, unhandled exception → failed; summarise_text respects max words and its tokens are included in the run's tokens used
