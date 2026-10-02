# 06: Loop resilience and summarise_text

**What to build:** The agent loop survives everything an LLM or a tool can throw at it. Hallucinated or disabled tool names, malformed or invalid arguments, tool exceptions and slow tools all become visible steps and the agent keeps going; multiple tool calls in one response run concurrently; an unexpected crash ends the run as failed with an explanation instead of leaving it stuck. The `summarise_text` tool lets the agent condense text via a nested LLM call whose tokens count toward the run.

**Blocked by:** 05 (Tracer run)

**Status:** done

- [x] Unknown or disabled tool, invalid JSON and schema-invalid arguments produce a `tool_result` with the error flag; every tool-call id still receives a tool message; the loop continues
- [x] Multiple tool calls in one LLM response: `tool_call` steps recorded in order before execution, tools run concurrently, `tool_result`/`tool_timeout` steps recorded, tool messages appended in the original call order
- [x] Tool timeout records a `tool_timeout` step (tool call id, name, timeout seconds) and the loop continues
- [x] Unhandled exception in the runner records an `error` step (type and message only; traceback in logs) and marks the run failed; `done` published with status failed
- [x] Tool enablement enforced at dispatch time, independent of which schemas were sent
- [x] `summarise_text` (async): calls the planner with no tools and a word-limit prompt, hard-truncates to `max_words`, adds its usage to the run's tokens used via the tool context
- [x] Tests (runner seam): hallucinated tool, disabled tool, malformed JSON, invalid args, tool exception, timeout, parallel calls preserve order and run concurrently, unhandled exception → failed; summarise_text respects max words and its tokens are included in the run's tokens used

## Comments

- From ticket 06: notes for later tickets.
  - **08:** tools in one LLM response now run concurrently and share one `ToolContext`. In tests, `worker_session_factory` binds every session to a single connection, so two `remember_fact` calls using `ctx.session_factory` at the same time would collide on it. Either serialise DB access in the test fixture or test `remember_fact` concurrency some other way.
  - **11:** `_AgentLoop.run()` (`app/runs/runner.py`) catches `Exception` to record `error` and fail the run, and `dispatch` also catches tool exceptions. Celery's `SoftTimeLimitExceeded` is an `Exception`, so once revocation with SIGUSR1 exists, both places must re-raise it. Otherwise a forced cancel shows up as an `error` step or a tool error, and the task's cancel cleanup never runs.
  - **11:** a run row deleted mid-flight currently goes through that same failure path: two exception logs, an `error` step that fails to write, and `lifecycle.fail` returning False. Tell that case apart there and stop quietly.
  - **Known gap:** if `summarise_text` times out during its nested LLM call, the provider may already have spent tokens that never reach `tokens_used`.
