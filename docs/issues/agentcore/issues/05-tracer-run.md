# 05: Tracer run — submit → worker → completed

**What to build:** A user submits a message to a session and instantly gets back a `run_id` (202) with links to its status and stream. A Celery worker picks the run up, executes the agent loop — LLM call, tool dispatch, feed results back, repeat — and finishes with a final answer. The user can poll status (including tokens used and step count) and fetch the full step-by-step trace. Session detail shows the five most recent runs. With the scripted fake LLM, "What is 15% of 3,655,000?" completes with a trace of llm_call → tool_call → tool_result → llm_call → final_answer.

**Blocked by:** 04 (Agent sessions)

**Status:** done

- [x] Agent runs table (spec columns plus `started_at`, `finished_at`; session FK `ON DELETE CASCADE`; indexes on session id and status) and run steps table (run FK `ON DELETE CASCADE`; index on run id); migration; relationships use `raise`
- [x] Fixed step-type set: memory_retrieval, llm_call, tool_call, tool_result, tool_timeout, final_answer, error, cancelled; payload shapes as in the spec (lean; usage and latency on llm_call; tool results truncated)
- [x] LLM planner abstraction returning a normalised result (content, tool calls, finish reason, usage, latency); OpenAI implementation (AsyncOpenAI, gpt-4o-mini, `tool_choice='auto'`) and a scripted fake whose responses are declared per test
- [x] Run lifecycle service: atomic claim `queued → running` via conditional update (sets started time); conditional terminal transitions that never overwrite a terminal status (set finished time)
- [x] Event publisher: persist step, then publish `{id, step_type, payload, occurred_at}` to the run's Redis channel; publishes terminal `done` with status
- [x] Runner: claim → build context (persona + user message) → up to 10 iterations of LLM call → dispatch tool calls (sequentially is fine here) → append tool messages; plain answer → final_answer step + completed; cap → final_answer "Max iterations reached" with cap flag + completed; usage added to tokens used per LLM call
- [x] Celery app (prefork) and `execute_agent_run(run_id)`: `asyncio.run` of the runner with a per-task async engine (`NullPool`) and per-task Redis client, disposed afterwards; a run not in `queued` state is a no-op
- [x] `POST /api/v1/sessions/{id}/run` `{message ≤ 8,000 chars}` → 202 `{run_id, status, status_url, stream_url}`; inserts run as queued, commits, enqueues with task id = run id; enqueue failure marks the run failed
- [x] `GET /api/v1/runs/{id}/status` → run id, status, tokens used, step count (count query), created/started/finished, final answer when completed
- [x] `GET /api/v1/runs/{id}/steps` → paginated steps in occurrence order
- [x] Session detail includes the last 5 run summaries via an explicit limited query
- [x] Owned-run lookup joins through the session's user id; another user's run → 404 on status and steps
- [x] Logs bind run id and step type in the worker
- [x] Tests: HTTP seam — submit, execute in-process via the task's entry point, status before/after, step trace shape, session detail recent runs, 404 isolation; runner seam — plain answer stops, 10-iteration cap, second invocation on a completed run is a no-op; every published event corresponds to an already-persisted step

## Comments

- From ticket 04: `DELETE /sessions/{id}` deletes via the ORM (`db.delete(agent_session)`). When adding `AgentSession.runs` with `lazy="raise"`, also set `passive_deletes=True` so the database `ON DELETE CASCADE` does the work instead of SQLAlchemy trying to load the runs. `SessionDetail.recent_runs` / `RunSummary` already exist and only need populating.
- From ticket 05: notes for later tickets.
  - **06:** an exception escaping `_AgentLoop.run()` (`app/runs/runner.py`) still leaves the run in `running`, with no `error` step and no `done`. Wrap `run_agent`'s loop, then use `lifecycle.fail` and `publish_done(..., FAILED)`.
  - **07:** events go to `run_channel(run_id)` (`app/runs/events.py`) as `{id, step_type, payload, occurred_at}`; `done` is `{step_type: "done", status}`. Step ids are a bigint sequence, so ordering by `(occurred_at, id)` matches insertion order.
  - **10:** without `acks_late`, a task that dies before the claim leaves its run `queued` forever.
  - **10:** a macOS host can't run the prefork pool (it uses spawn, which breaks Celery's `fast_trace_task`), so use `--pool=solo` for local runs outside Docker. The Linux containers fork and are fine.
  - **11:** `lifecycle.complete` already refuses to overwrite a non-running run, and the runner then skips publishing `done`. `set_tokens_used` only touches `running` rows.
  - **13:** `create_llm_provider` in `app/llm/__init__.py` currently returns `ScriptedLLM(fallback=...)` with a canned reply. Replace it with the Dubai scenario player.
  - **API:** a failed enqueue returns 503 `service_unavailable` with `details.run_id`, and publishes `done`/failed.
