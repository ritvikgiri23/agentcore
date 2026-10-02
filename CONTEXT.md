# AgentCore glossary

The terms the code, API and docs use. Prefer these over synonyms.

- **User**: an account (email + password hash). Owns sessions and long-term memories.
  Every owned resource is invisible to other users: 404, never 403.
- **Agent session** (`agent_sessions`, `/sessions`): a reusable persona, made of a name,
  a system prompt and the tools it may use (`tools_enabled`). Not a login session.
- **Agent run** (`agent_runs`, `/runs`): one user message submitted to a session and
  executed by a worker. Status is one of `queued`, `running`, `completed`, `failed`,
  `cancelled`.
  - **Active** means queued or running.
  - **Terminal** means the other three.
- **Run step** (`run_steps`): an append-only record of one thing that happened in a
  run. Its type is one of `memory_retrieval`, `llm_call`, `tool_call`, `tool_result`,
  `tool_timeout`, `final_answer`, `error`, `cancelled`. The ordered steps are the run's
  **trace**.
- **`done` event**: the last event on a run's stream, carrying the terminal status.
  Never persisted, and not a step.
- **Iteration**: one LLM call in a run, plus the tool calls it requested. Capped at 10.
- **Tool**: a function registered with `@tool`. Run only through the **dispatcher**,
  which turns every failure into an error result instead of raising.
- **Tool context** (`ToolContext`): server-side context (user, run, database, LLM,
  token usage) injected into a tool. Never part of the schema the LLM sees.
- **Short-term memory**: a session's recent conversation **turns** (user message +
  final answer) in a Redis list. 20 are retained; the last 5 are used as context.
- **Long-term memory** (`long_term_memory`, `/memory`): durable facts about a user,
  embedded in pgvector. Written by the `remember_fact` tool and recalled by similarity
  at the start of each run. Outlives sessions.
- **Claim**: the atomic `queued → running` transition that decides which worker
  executes a run.
- **Lease**: a Redis key with a TTL. A worker holds it while executing a run and
  refreshes it every iteration. A `running` run without a lease has a **lost worker**.
- **Admission**: taking one of a user's active-run slots before a run is inserted.
  Refused with 429 at the limit (10).
- **Cancel flag**: a Redis key holding the cancellation reason. The worker checks it at
  every **step boundary**.
- **Settle**: finishing a cancelled run's record: one `cancelled` step, then `done`.
- **Fake mode**: running with the deterministic demo LLM and hash embedder, used when
  no OpenAI key is set.
