# 05: Tracer run — submit → worker → completed

**What to build:** A user submits a message to a session and instantly gets back a `run_id` (202) with links to its status and stream. A Celery worker picks the run up, executes the agent loop — LLM call, tool dispatch, feed results back, repeat — and finishes with a final answer. The user can poll status (including tokens used and step count) and fetch the full step-by-step trace. Session detail shows the five most recent runs. With the scripted fake LLM, "What is 15% of 3,655,000?" completes with a trace of llm_call → tool_call → tool_result → llm_call → final_answer.

**Blocked by:** 04 (Agent sessions)

**Status:** ready-for-agent

- [ ] Agent runs table (spec columns plus `started_at`, `finished_at`; session FK `ON DELETE CASCADE`; indexes on session id and status) and run steps table (run FK `ON DELETE CASCADE`; index on run id); migration; relationships use `raise`
- [ ] Fixed step-type set: memory_retrieval, llm_call, tool_call, tool_result, tool_timeout, final_answer, error, cancelled; payload shapes as in the spec (lean; usage and latency on llm_call; tool results truncated)
- [ ] LLM planner abstraction returning a normalised result (content, tool calls, finish reason, usage, latency); OpenAI implementation (AsyncOpenAI, gpt-4o-mini, `tool_choice='auto'`) and a scripted fake whose responses are declared per test
- [ ] Run lifecycle service: atomic claim `queued → running` via conditional update (sets started time); conditional terminal transitions that never overwrite a terminal status (set finished time)
- [ ] Event publisher: persist step, then publish `{id, step_type, payload, occurred_at}` to the run's Redis channel; publishes terminal `done` with status
- [ ] Runner: claim → build context (persona + user message) → up to 10 iterations of LLM call → dispatch tool calls (sequentially is fine here) → append tool messages; plain answer → final_answer step + completed; cap → final_answer "Max iterations reached" with cap flag + completed; usage added to tokens used per LLM call
- [ ] Celery app (prefork) and `execute_agent_run(run_id)`: `asyncio.run` of the runner with a per-task async engine (`NullPool`) and per-task Redis client, disposed afterwards; a run not in `queued` state is a no-op
- [ ] `POST /api/v1/sessions/{id}/run` `{message ≤ 8,000 chars}` → 202 `{run_id, status, status_url, stream_url}`; inserts run as queued, commits, enqueues with task id = run id; enqueue failure marks the run failed
- [ ] `GET /api/v1/runs/{id}/status` → run id, status, tokens used, step count (count query), created/started/finished, final answer when completed
- [ ] `GET /api/v1/runs/{id}/steps` → paginated steps in occurrence order
- [ ] Session detail includes the last 5 run summaries via an explicit limited query
- [ ] Owned-run lookup joins through the session's user id; another user's run → 404 on status and steps
- [ ] Logs bind run id and step type in the worker
- [ ] Tests: HTTP seam — submit, execute in-process via the task's entry point, status before/after, step trace shape, session detail recent runs, 404 isolation; runner seam — plain answer stops, 10-iteration cap, second invocation on a completed run is a no-op; every published event corresponds to an already-persisted step
