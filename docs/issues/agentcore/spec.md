# AgentCore — Specification

Status: ready-for-agent

## Problem Statement

Users want to delegate multi-step tasks to an AI agent in plain English — "What is 15% of the current population of Dubai, and remember that fact for me?" — and have the agent figure out which tools to use, use them, and come back with an answer. Today there is no backend that lets them:

- define reusable agent personas with a controlled set of tools,
- submit a task without blocking on a long-running LLM conversation,
- watch the agent reason step by step in real time, and replay that reasoning later,
- have the agent carry context from recent conversations and remember durable facts about them across sessions,
- trust that their sessions, runs and memories are invisible to every other user,
- stop a runaway or unwanted run, and not be able to accidentally overload the system.

## Solution

AgentCore is a versioned HTTP API (all routes under `/api/v1`) backed by PostgreSQL + pgvector, Redis, and a Celery worker pool.

A user registers and receives a JWT. They create **agent sessions** — a name, a system prompt (persona) and a list of enabled tools validated against a tool registry. They submit a message to a session; the API instantly returns a `run_id` with links to poll status and stream progress. A Celery worker picks up the **agent run** and executes a ReAct-style loop: assemble context (persona + recent short-term conversation turns + relevant long-term memories), call the LLM with the session's tool definitions, dispatch any requested tool calls, feed results back, and repeat until the LLM produces a plain answer or a 10-iteration safety cap is hit.

Every action is persisted as an append-only **run step** and published live to a per-run Redis channel. A Server-Sent Events endpoint replays persisted steps and then relays live ones, so a client that connects late misses nothing. Completed turns are appended to Redis short-term memory; facts the agent chooses to remember are embedded and stored in pgvector long-term memory, which users can list, semantically search and delete.

Runs can be cancelled (cooperatively between steps, and forcibly via Celery revocation), are idempotent under broker redelivery, survive worker crashes without getting stuck, and are rate-limited to 10 active runs per user. When no OpenAI key is configured, a deterministic fake LLM and embedder let the entire system be demoed end to end.

## User Stories

### Authentication
1. As a new user, I want to register with an email and password and immediately receive an access token, so that I can start using the API without a separate login step.
2. As a returning user, I want to log in with my credentials and receive an access token, so that I can resume using my sessions.
3. As a user, I want my access token to expire after 30 minutes, so that a leaked token has a bounded blast radius.
4. As a user, I want a clear 401 error when my token is missing, malformed or expired, so that my client knows to re-authenticate.
5. As a user, I want registering with an already-used email to fail with a clear conflict error, so that I don't silently create duplicate accounts.
6. As a user, I want my password stored only as a secure hash, so that a database leak does not expose my credentials.

### Agent sessions
7. As a user, I want to create an agent session with a name, system prompt and list of enabled tools, so that I can define a reusable agent persona.
8. As a user, I want session creation to fail with a clear 422 listing the unknown tool names and the available ones when I request a tool that doesn't exist, so that I can fix my request immediately.
9. As a user, I want duplicate tool names in my request to be silently de-duplicated, so that minor client mistakes don't cause errors.
10. As a user, I want sensible size limits on session name and system prompt enforced with clear validation errors, so that I can't accidentally submit unusable input.
11. As a user, I want to list my sessions with pagination, so that I can browse them even when I have many.
12. As a user, I want to view a session's details including its five most recent run summaries, so that I can see what the agent has been doing at a glance.
13. As a user, I want to delete a session and have all its runs and their steps deleted with it, so that I can clean up personas I no longer need.
14. As a user, I want deleting a session to first cancel any of its queued or running runs, so that no orphaned work keeps running against a deleted session.
15. As a user, I want deleting a session to clear its short-term conversation history, so that no stale context lingers.
16. As a user, I want my long-term memories to survive deleting the session that created them, so that durable facts about me are not lost when I reorganise personas.
17. As a user, I want any attempt to view or delete another user's session to return 404, so that I cannot even learn whether that session exists.

### Submitting and tracking runs
18. As a user, I want to submit a message to a session and get a `run_id` back instantly (202 Accepted) with links to its status and stream, so that my client never blocks on the agent.
19. As a user, I want message size limits enforced with a clear validation error, so that I don't submit something the agent cannot process.
20. As a user, I want to poll a run's status and see its status, tokens used, step count, timestamps (created, started, finished) and — once completed — the final answer, so that a simple polling client needs only one call.
21. As a user, I want to fetch the full, paginated step-by-step trace of a run — memory retrieval, LLM calls, tool calls, tool results, timeouts, errors, final answer — so that I can understand and audit how the agent reached its answer.
22. As a user, I want each LLM-call step to show the model, finish reason, any text content, the tool calls requested, token usage and latency, so that I can understand cost and behaviour without the trace being bloated by the full prompt each time.
23. As a user, I want tool results in the trace to be truncated if very large, so that the trace stays readable and the stream stays light.
24. As a user, I want any attempt to access another user's run (status, steps, stream, cancel) to return 404, so that run data is fully isolated.
25. As a user, I want to receive a 429 when I already have 10 queued or running runs, so that I understand why my submission was rejected and the system stays healthy.
26. As a user, I want the active-run limit to recover automatically if its bookkeeping drifts, so that I am never permanently locked out by a stale counter.

### Real-time streaming
27. As a user, I want to open an SSE stream for a run and receive each step as a JSON event the moment it happens, so that I can watch the agent think in real time.
28. As a user, I want a stream opened after some steps have already happened (or after the run finished) to first replay all persisted steps in order, so that I never miss events because I connected late.
29. As a user, I want each step delivered exactly once on the stream even when replay and live delivery overlap, so that my UI doesn't show duplicates.
30. As a user, I want a final `done` event carrying the terminal status (completed, failed or cancelled) and then the stream to close, so that my client knows the run is over.
31. As a browser-based client, I want to authenticate the stream via an `access_token` query parameter as well as the Authorization header, so that I can use the native EventSource API, which cannot send headers.
32. As a user, I want periodic keepalive comments on idle streams, so that proxies and load balancers don't drop my connection during long LLM calls.
33. As an operator, I want client disconnects to unsubscribe from Redis and end quietly without error logs, so that abandoned streams don't leak resources or pollute logs.

### Agent behaviour
34. As a user, I want the agent to see its persona, my last five conversation turns in this session and up to three relevant long-term memories, so that it answers with appropriate context.
35. As a user, I want irrelevant long-term memories (beyond a configurable distance threshold) to be left out of the prompt, so that noise doesn't degrade answers.
36. As a user, I want the trace to show which memories were retrieved and how relevant they were, so that I can understand why the agent "knew" something.
37. As a user, I want the agent to keep calling tools and reasoning until it has a final answer, so that it can complete multi-step tasks like search → calculate → remember.
38. As a user, I want the agent to stop after 10 iterations with the final answer "Max iterations reached", so that a confused agent can't run forever or burn unlimited tokens.
39. As a user, I want the agent to recover when the LLM requests a tool that doesn't exist or isn't enabled for this session — receiving an error result listing available tools — so that a hallucinated tool name doesn't crash my run.
40. As a user, I want the agent to recover when the LLM sends malformed or schema-invalid tool arguments, so that a formatting slip becomes a correctable error, not a failed run.
41. As a user, I want any exception inside a tool to come back to the agent as an error string, so that one broken tool never crashes the loop.
42. As a user, I want a tool that takes longer than 5 seconds to be abandoned with a recorded timeout step while the loop continues, so that one slow tool doesn't stall my run.
43. As a user, I want multiple independent tool calls in a single LLM response to run concurrently, so that my run finishes faster.
44. As a user, I want transient LLM provider errors (rate limits, 5xx) retried with exponential backoff, so that brief provider hiccups don't fail my run.
45. As a user, I want a run that fails unexpectedly to end with status `failed` and an error step explaining what went wrong, so that I am never left with a run stuck in `running`.
46. As a user, I want the run's total token usage — including nested LLM calls made by tools such as summarisation — to be tracked, so that I can see the true cost of a run.
47. As a user, I want the agent to continue without long-term memories if the embedding call fails at the start of a run, so that a memory outage doesn't block answering.

### Tools
48. As a user, I want a `web_search` tool that returns three search result snippets, so that the agent can look up information (mocked deterministically for this build).
49. As a user, I want `web_search` to return realistic, number-bearing results for known topics such as Dubai's population, so that the flagship multi-step scenario works end to end.
50. As a user, I want a `calculator` tool that safely evaluates arithmetic expressions, so that the agent computes numbers accurately instead of guessing.
51. As an operator, I want the calculator to reject anything other than numbers and arithmetic operators — and guard against pathological inputs like huge exponents — so that it can never be used to execute code or exhaust the worker.
52. As a user, I want a `get_current_datetime` tool returning the current UTC timestamp, so that the agent can reason about time.
53. As a user, I want a `summarise_text` tool that uses the LLM to summarise text within a word limit, so that the agent can condense long material.
54. As a user, I want a `remember_fact` tool that stores a fact in my long-term memory, so that the agent can remember things about me across sessions.
55. As a user, I want `remember_fact` to skip storing a near-duplicate of a fact it already remembers, so that repeated questions don't clutter my memory and crowd out retrieval.
56. As a developer, I want to add a new tool by writing a single decorated, type-hinted function with a docstring, so that the tool's schema, registration, validation and session-level availability all follow automatically.
57. As a security reviewer, I want server-side context (user, run, database, LLM) injected into tools through a hidden parameter that never appears in the LLM-facing schema, so that the LLM can never choose or forge which user a tool acts on.
58. As a security reviewer, I want tool enablement enforced at dispatch time as well as by which schemas are sent, so that prompt injection cannot invoke a tool the session didn't enable.

### Memory
59. As a user, I want each completed run's message and final answer saved as a conversation turn in my session's short-term memory, so that follow-up questions have context.
60. As a user, I want only successfully completed runs (not failed, cancelled or max-iteration runs) saved to short-term memory, so that broken turns don't pollute future context.
61. As an operator, I want short-term memory retained to the last 20 turns per session while only the last 5 turns are used as context, so that the context window stays bounded and old turns are evicted automatically.
62. As a user, I want to list my long-term memories with pagination, so that I can see everything the agent remembers about me.
63. As a user, I want to semantically search my long-term memories with a plain-text query and get the top five matches with their relevance, so that I can inspect what the agent would recall about a topic.
64. As a user, I want to delete a specific long-term memory, so that I can make the agent forget something.
65. As a user, I want memory search, listing and deletion to only ever touch my own memories, so that nobody else can read or erase what the agent knows about me.
66. As a user, I want each memory to record which run created it, so that I can trace where a fact came from.

### Cancellation
67. As a user, I want to cancel a queued run so that it never starts.
68. As a user, I want to cancel a running run and have it stop at the next step boundary, so that I stop paying for work I don't want.
69. As a user, I want a run blocked inside a long LLM or tool call to be forcibly stopped by revoking its worker task, so that cancellation works even when the loop can't reach a checkpoint.
70. As a user, I want a cancelled run to end with status `cancelled`, a cancellation step and a `done` event on the stream, so that every client sees a consistent outcome.
71. As a user, I want cancelling a run that has already finished to fail with a clear conflict error, so that I understand nothing happened.
72. As a user, I want a cancelled run never to be overwritten to `completed` by a worker that was finishing up, so that the status I see is the truth.

### Reliability
73. As an operator, I want a run delivered twice by the broker to execute only once, enforced by an atomic claim from `queued` to `running`, so that duplicate deliveries never cause double tool calls or duplicate memories.
74. As an operator, I want a redelivered task for an already completed, failed or cancelled run to do nothing, so that retries are safe.
75. As an operator, I want a run whose worker died mid-loop to be detected via an expired heartbeat lease and marked `failed` with a "worker lost" error on redelivery — not resumed — so that runs never stay stuck in `running` and side-effecting tools are never replayed.
76. As an operator, I want failures that occur before a run is claimed (e.g. database not yet reachable) retried by Celery with backoff, so that startup races self-heal.
77. As an operator, I want every terminal transition to release the user's rate-limit slot exactly once, so that the active-run limit stays accurate.
78. As an operator, I want the runner to stop quietly if its run is deleted mid-flight, so that session deletion never produces worker crashes.

### Errors, observability and API quality
79. As an API consumer, I want every error — validation, not found, conflict, rate limit, auth, unexpected server error — returned in one consistent envelope with a machine-readable code, message, details and request id, so that I can handle errors uniformly.
80. As an API consumer, I want unexpected server errors to return a generic message without internal details, so that the system doesn't leak implementation information.
81. As an API consumer, I want every response to carry a request id header (echoing mine if I sent one), so that I can correlate my calls with server logs.
82. As an operator, I want structured JSON logs that carry request id, user id, session id, run id and step type wherever applicable, so that I can trace any run across the API and workers.
83. As an operator, I want access tokens passed in query strings scrubbed from request logs, so that SSE authentication doesn't leak credentials into logs.
84. As an API consumer, I want every route documented in OpenAPI with response models, status codes, error responses and summaries, so that the Swagger UI is usable as-is.

### Running and demoing
85. As a reviewer, I want a single `docker compose up` to start Postgres (with pgvector), Redis, run migrations, then start the API and a Celery worker in the correct order, so that a fresh clone just works.
86. As a reviewer without an OpenAI key, I want the system to fall back to a deterministic fake LLM and embedder that play the Dubai scenario end to end, so that I can see every feature working at zero cost.
87. As a reviewer with an OpenAI key, I want to put it in an env file and have the system use `gpt-4o-mini` and `text-embedding-3-small`, so that I can see real agent behaviour.
88. As a reviewer, I want a small demo page served by the API that registers, creates a session, submits the Dubai prompt and renders streamed steps live, so that I can watch the agent think in a browser.
89. As a reviewer, I want Makefile shortcuts for starting, testing, migrating and demoing, so that common workflows are one command.
90. As a reviewer, I want a README quickstart with a curl walkthrough, a NOTES document explaining the agent loop, memory strategy, deviations from the brief and future improvements, and ADRs for the biggest decisions, so that I can evaluate the thinking as well as the code.

## Implementation Decisions

### Modules

- **Config** — Pydantic Settings. Holds OpenAI key (optional), JWT secret and expiry, database and Redis URLs, LLM provider (`openai` | `fake`, defaulting to `fake` when no key is set), model names, and tunables: max iterations (10), tool timeout (5s), short-term context messages (10), short-term retained turns (20), long-term top-k (3), memory max distance (permissive default), memory dedup distance (0.05), max active runs per user (10), lease TTL (~2 min), tool result truncation (~4 KB).
- **Security** — password hashing (passlib) and JWT encode/decode (python-jose). Access tokens only; 30-minute expiry.
- **Dependencies** — `get_db`, `get_redis`, `get_current_user` (Bearer header only), and `get_current_user_sse` (Bearer header *or* `access_token` query parameter; used only by the stream route).
- **Ownership repository** — one place for owned-resource lookups (`get_owned_session`, `get_owned_run`, `get_owned_memory`). Every lookup filters by the caller's user id in the query (runs and steps via a join to the session's user id). Not found and not owned are indistinguishable → 404.
- **Errors** — an application error base (code, message, HTTP status, details) with subclasses for not found, conflict, rate limited, unauthorized, etc. Exception handlers render application errors, request validation errors, HTTP exceptions and unhandled exceptions into one envelope: `{error: {code, message, details, request_id}}`. A request-id middleware reads or creates `X-Request-ID`, echoes it, and binds it into the log context.
- **Logging** — structlog JSON output with context variables for request id, user id, session id, run id and step type; query-string access tokens scrubbed.
- **LLM planner** — a provider abstraction exposing a chat-completion call (messages, tool definitions, `tool_choice='auto'`) that returns a normalised result (content, tool calls, finish reason, usage, latency), and an embedding call returning 1536-dim vectors. Two implementations: OpenAI (AsyncOpenAI, `gpt-4o-mini`, `text-embedding-3-small`, SDK retries plus a small exponential-backoff-with-jitter wrapper, ~3 attempts, for 429/5xx/timeouts) and Fake (a scripted LLM that plays the Dubai scenario and a hash-based deterministic embedder). The same fakes are used by the test suite.
- **Tool registry & dispatcher** — a `@tool` decorator builds a Pydantic model from the function's type hints (per-parameter descriptions via annotated fields), takes the description from the docstring, generates the OpenAI function schema from the model's JSON schema, and registers it in a module-level registry. A parameter typed as `ToolContext` is injected by the dispatcher and excluded from the schema. `ToolContext` carries user id, run id, a database session factory, the LLM provider and a token-usage accumulator. Tools may be sync (run via a worker thread) or async (awaited directly); every invocation is wrapped in a 5-second timeout. The dispatcher takes a tool name, a JSON argument string, the session's enabled tools and the context. It returns a structured outcome (result string, error flag, timed-out flag, latency) and never raises: unknown tool, tool not enabled, invalid JSON, schema validation failure, tool exception and timeout all become error outcomes with a helpful message (unknown/disabled lists the available tools).
- **Tools** — `web_search` (deterministic keyword fixture: Dubai population, weather, python, etc., with a generic fallback echoing the query; three snippets with title/url/snippet); `calculator` (whitelisted AST walker: numeric constants, `+ - * / // % **`, unary minus; rejects everything else; caps exponent size and input length; division by zero → error); `get_current_datetime` (UTC ISO timestamp); `summarise_text` (async; calls the planner with no tools and a word-limit prompt, hard-truncates as a safety net, adds its usage to the run's token count); `remember_fact` (async; embeds, checks the user's nearest memory against the dedup distance, returns "Already remembered: …" or inserts with source run id and returns "Remembered: …").
- **Memory manager** — short-term: push a completed turn `{user, assistant, run_id, ts}` onto the session's Redis list, trim to the retained-turn limit, read the most recent turns needed for the context window (5 turns = 10 messages), reversed into chronological order, and delete a session's history. Long-term: embed and insert, nearest-neighbour search for one user with an optional distance cutoff, list, delete, and the dedup check.
- **Event publisher** — persists a run step, *then* publishes `{id, step_type, payload, occurred_at}` to the run's Redis channel. Persistence before publish is a hard invariant (Postgres is the source of truth; pub/sub is the live tail). Also publishes the terminal `done` event.
- **Run lifecycle service** — the only place run status changes. Atomic claim (`queued → running` via a conditional update returning the row; sets started time), conditional terminal transitions (`running → completed | failed`, `queued|running → cancelled`; sets finished time; never overwrites a terminal status), heartbeat lease (Redis key with TTL, refreshed each iteration), cancel flag (Redis key checked before each LLM call and each tool dispatch), and rate-limit slot release, called on every terminal transition.
- **Rate limiter** — per-user Redis set of active run ids. Admission is an atomic Lua script (add the run id only if the set has fewer than the limit). Release is an idempotent set removal. When admission fails, reconcile the set once against Postgres (runs with status queued or running) and retry before returning 429.
- **Agent runner** — `run_agent(run_id)` orchestrates the loop: claim → build context (persona; up to 3 long-term memories, filtered by distance, appended to the system message in a delimited "relevant memories" block with a may-be-outdated caveat; recent short-term turns; the new user message) → record `memory_retrieval` → up to 10 iterations of: check cancel flag and refresh lease → LLM call → record `llm_call` and add usage to tokens used → if no tool calls, finish; else record each `tool_call` in order, dispatch all concurrently, append tool messages in original call order (every tool-call id gets a reply), and record each `tool_result` / `tool_timeout`. On a plain answer: record `final_answer`, complete the run, push the turn to short-term memory. On cap: record `final_answer` "Max iterations reached" with a cap flag, complete the run, do **not** push to short-term memory. On unhandled exception: record `error` (type + message; traceback in logs only) and mark failed. On a cancel flag: record `cancelled` and stop. If the run row disappears mid-flight, stop quietly. Always publish `done` with the terminal status.
- **Celery app & task** — prefork pool; `acks_late` and reject-on-worker-lost enabled. `execute_agent_run(run_id)` runs `asyncio.run(run_agent(run_id))` with a per-task async engine (`NullPool`) and a per-task Redis client, both disposed afterwards (asyncpg connections are bound to their event loop). The Celery task id equals the run id. On redelivery finding `running` with an expired lease: mark failed with a "worker lost" error; never resume. Pre-claim infrastructure failures use Celery autoretry with backoff. On `SoftTimeLimitExceeded` (raised by SIGUSR1 revocation) the task performs cleanup in a fresh short event loop: mark cancelled, record `cancelled`, publish `done`, release the slot.
- **Routers** — auth, sessions, runs, stream, memory, plus a static demo page at `/demo`. All mounted under `/api/v1` (the demo page excepted). Every route declares response model, status code, error responses and summary.

### Schema (deviations from the brief are deliberate)

- **users**: id (uuid string), email (unique, indexed), hashed password, created at.
- **agent_sessions**: as specified; index on user id. Relationship loading changed from `selectin` to `raise`: the brief's eager loading would pull every run and every step whenever a session is read.
- **agent_runs**: as specified, **plus** `started_at` and `finished_at`. Indexes on session id and status. Session foreign key `ON DELETE CASCADE`. Status stored as the specified enum (queued, running, completed, failed, cancelled). Relationship loading `raise`.
- **run_steps**: as specified; index on run id (plus ordering by occurrence time and insertion). Run foreign key `ON DELETE CASCADE`. `step_type` takes values from a fixed set: `memory_retrieval`, `llm_call`, `tool_call`, `tool_result`, `tool_timeout`, `final_answer`, `error`, `cancelled`. `done` is never persisted.
- **long_term_memory**: as specified; B-tree index on user id; HNSW index on embedding with `vector_cosine_ops` (must match the `<=>` operator). Source run foreign key `ON DELETE SET NULL`.
- One Alembic migration that also runs `CREATE EXTENSION IF NOT EXISTS vector`.

### Step payloads

| step_type | payload |
|---|---|
| `memory_retrieval` | memories (id, content, distance), short-term turn count |
| `llm_call` | iteration, model, finish reason, content, tool calls (id, name, arguments), usage (prompt, completion, total), latency |
| `tool_call` | iteration, tool call id, name, arguments |
| `tool_result` | tool call id, name, result (truncated), error flag, latency |
| `tool_timeout` | tool call id, name, timeout seconds |
| `final_answer` | content, iterations, max-iterations-hit flag |
| `error` | error type, message |
| `cancelled` | reason |

### API contracts (all under `/api/v1`)

- `POST /auth/register` → 201 `{access_token, token_type, expires_in}`; 409 on duplicate email.
- `POST /auth/token` → 200 same shape (OAuth2 password form). Access token only — the brief's mention of a refresh token contradicts its own feature section; noted in NOTES.
- `GET /sessions` → paginated `{items, total, limit, offset}` (limit default 50, max 200).
- `POST /sessions` → 201 session; 422 `unknown_tool` with details listing unknown and available tools. Name 1–100 chars, system prompt ≤ 4,000 chars.
- `GET /sessions/{id}` → session plus the last 5 run summaries (id, status, user message truncated to 120 chars, tokens used, created/finished times), fetched with an explicit limited query.
- `DELETE /sessions/{id}` → 204. Cancels active runs through the cancellation path, deletes the session (cascading to runs and steps), deletes its short-term history. Memories are kept.
- No session update endpoint (not in the brief's endpoint table).
- `POST /sessions/{id}/run` `{message ≤ 8,000 chars}` → 202 `{run_id, status: "queued", status_url, stream_url}`. Order: rate-limit admission → insert run (queued) → commit → enqueue with task id = run id. If enqueue fails, mark the run failed and release the slot. 429 when at the limit.
- `GET /runs/{id}/status` → `{run_id, status, tokens_used, step_count, created_at, started_at, finished_at, final_answer?}`; step count via a count query.
- `GET /runs/{id}/steps` → paginated steps in occurrence order.
- `GET /runs/{id}/stream` → `text/event-stream`. Ownership check before subscribing. Algorithm: subscribe to the run channel (buffering) → replay persisted steps in order, remembering their ids → if the run is terminal, emit `done` and close → otherwise relay live events, skipping already-sent ids, until the terminal event, then emit `done` and close. Frames: `data: {"step_type": …, "payload": …}\n\n`; final `data: {"step_type": "done", "status": "<terminal status>"}\n\n`. Keepalive comment ~every 15s. Disconnect detection → unsubscribe and return without error logging.
- `DELETE /runs/{id}` → 202 (or 200) with the run's new status; 409 if already terminal. Atomic transition to cancelled, set the cancel flag, revoke with terminate and SIGUSR1, release the slot.
- `GET /memory` → paginated memories (id, content, source run id, created at).
- `GET /memory/search?q=` → top 5 `{id, content, distance, source_run_id, created_at}` for the caller only.
- `DELETE /memory/{id}` → 204.

### Infrastructure

- Compose services: `postgres` (`pgvector/pgvector:pg16`, healthcheck, init script that also creates the test database), `redis` (`redis:7-alpine`, healthcheck), `migrate` (one-shot Alembic upgrade; waits for a healthy Postgres), `api` and `worker` (same image; both wait for `migrate` to complete successfully). Worker uses the prefork pool, concurrency 4.
- One Python 3.12-slim image with dependencies managed by `uv` and a lockfile.
- A dev override file mounts source and enables reload; the base compose file stays production-like.
- `.env.example` documents all settings; `.env` is gitignored.
- Makefile targets: up, test, migrate, demo.

### Delivery

- Public GitHub repository; small, meaningful commits in build order (scaffold & compose → models & migration → auth → sessions & tool registry → planner & runner → Celery → SSE → memory → bonuses → docs).
- README (quickstart, `/api/v1` mapping, curl walkthrough of the Dubai scenario, tests, fake mode), NOTES (agent loop design, memory strategy, every deviation from the brief with rationale, prepared answers to the review questions, future work), ADRs for: the Celery/asyncio bridge, SSE replay-then-live, atomic claim + heartbeat lease, tool registry design, rate-limit set + Lua admission.

## Testing Decisions

### What makes a good test here

- Tests assert **external behaviour** through the highest available seam: HTTP responses, persisted run status and steps, SSE frames, Redis-visible memory, tool outcome strings. They do not assert on private helpers, call counts of internal functions, or the shape of intermediate data structures.
- Only two boundaries are faked: the **LLM provider** (scripted fake whose responses are declared per test, e.g. "call web_search, then calculator, then answer") and the **embedder** (deterministic hash-based vectors, so semantically identical text maps to identical vectors and similarity ranking is reproducible). Postgres with pgvector is real; Redis is async `fakeredis` (lists, sets, pub/sub, Lua).
- No test ever calls OpenAI.
- Each test runs inside a transaction that is rolled back; tables are created once per test session in a dedicated test database on the compose Postgres.
- Factory fixtures create users (with tokens), sessions and runs in specific states.
- Happy and failure paths are both covered; target ≥ 75% line coverage.

### Seams (agreed)

1. **HTTP API — primary seam.** An async HTTP client against the app, with the fake provider injected through settings/dependency overrides. Runs are executed in-process by invoking the same entry point the Celery task uses, rather than through a broker. Covers:
   - register/login/expired-token/missing-token paths;
   - session create (valid, unknown tool 422 with details, de-duplication, size limits), list pagination, detail with five recent runs, delete (cascade, active-run cancellation, short-term history removal, memories kept);
   - cross-user isolation: every session, run, step, stream and memory route returns 404 for another user's resource;
   - submit run → 202 with links; status before and after execution; step trace for the Dubai scenario (memory retrieval → llm call → web_search call/result → llm call → calculator call/result → llm call → remember_fact call/result → final answer);
   - short-term memory written after completion and used as context on the next run; not written for failed, cancelled or max-iteration runs;
   - memory list/search (top-5 ordering by deterministic embedding)/delete;
   - rate limiting: 11th active run → 429; slot released on completion, failure and cancellation; drifted set reconciled from Postgres;
   - cancellation: queued run cancelled and never executes; already-terminal run → 409;
   - SSE: replay-then-done for a finished run; live delivery for an in-progress run without duplicates; ownership 404; query-parameter token accepted; disconnect handled quietly;
   - error envelope shape for 401, 404, 409, 422, 429 and an unexpected 500 (no internals leaked); request id echoed.
2. **Agent runner — secondary seam** (`run_agent(run_id)` with the scripted provider). Covers loop behaviour awkward to drive via HTTP:
   - stops on a plain answer; stops at the 10-iteration cap with "Max iterations reached" and status completed;
   - hallucinated tool and disabled tool → error result listing available tools, loop continues;
   - malformed JSON and schema-invalid arguments → error result, loop continues;
   - parallel tool calls run concurrently and tool messages preserve the original call order;
   - tool timeout → `tool_timeout` step, loop continues;
   - cancel flag set mid-loop → `cancelled` step, status cancelled, no overwrite to completed;
   - idempotency: a second invocation on a completed/failed/cancelled run does nothing; two concurrent invocations on a queued run execute exactly once (atomic claim);
   - expired lease on a running run → failed with "worker lost";
   - unhandled exception → error step and status failed;
   - embedding failure during retrieval → run proceeds without memories;
   - nested summarisation usage included in tokens used;
   - every published event corresponds to an already-persisted step (persist-then-publish).
3. **Tool dispatcher — tertiary seam** (`dispatch(name, args_json, enabled_tools, ctx)`). Covers:
   - auto-generated schemas match function signatures and docstrings, and exclude `ToolContext`;
   - a newly decorated function becomes available without any other change (the "add get_weather" review exercise);
   - calculator accepts arithmetic and rejects names, calls, attributes, imports, huge exponents, over-long input; division by zero → error string;
   - any tool exception becomes an error string; sync and async tools both dispatch;
   - remember_fact dedups near-identical facts and stores source run id; web_search returns the Dubai fixture.
- **Celery wrapper** — one thin test that the task bridges to the runner, uses task id = run id at enqueue, and on `SoftTimeLimitExceeded` marks the run cancelled, publishes `done` and releases the slot.

### Prior art

The repository is greenfield; there are no existing tests. These seams and fixtures (async client, test database with per-test rollback, fake provider, fake embedder, user/session/run factories) establish the patterns all later tests should follow.

## Out of Scope

- Refresh tokens, token revocation, email verification, password reset.
- Session update (PATCH) endpoint.
- Real web search providers (SerpAPI/Tavily) — mocked; noted as future work.
- Resuming a run after a worker crash (runs are failed, not resumed, because tools have side effects).
- Single-use SSE stream tickets (documented as the production upgrade over query-string tokens).
- Redis Streams instead of pub/sub for event delivery.
- Per-user partitioning of long-term memory or iterative HNSW scans (documented as scale mitigations).
- Multi-tenant/org model, admin roles, quotas beyond the active-run limit, billing.
- A real frontend beyond the single demo page.
- Running a real Celery worker and broker inside the automated test suite.
- Deployment beyond docker compose (Kubernetes, CI/CD pipelines).

## Further Notes

- **Deviations from the brief**, each to be justified in NOTES: access token only despite the endpoint table's "access + refresh"; `ast.literal_eval` cannot evaluate operators, so the calculator uses a whitelisted AST walker in the same spirit; `remember_fact` and `summarise_text` are async tools despite "all tools are synchronous"; relationships use `raise` instead of `selectin`; extra step types; extra `started_at`/`finished_at` columns; all routes prefixed `/api/v1`; the rate-limit "counter" is a Redis set whose cardinality is the count.
- **Review-question answers to prepare**: how the loop stops (plain answer, 10-iteration cap, cancel flag); hallucinated tools (error result, loop continues, dispatch-time enablement check); tracing a run from the POST to the SSE close (Redis admission → Postgres insert → broker enqueue → worker atomic claim in Postgres → each step written to Postgres then published to Redis → SSE subscribes to Redis, replays from Postgres, relays live → terminal status in Postgres → turn pushed to Redis → `done` → close); pgvector cost at 500 rows (B-tree on user id + exact scan is sub-millisecond; HNSW plus the filtered-ANN fewer-than-k caveat at scale); concurrency and isolation (atomic claim, Lua admission, per-task engines, query-level user id filters, context injected server-side into tools).
- **Flagship scenario**: session "Research Assistant" with `web_search`, `calculator`, `remember_fact`; message "What is 15% of the current population of Dubai, and remember that fact for me?". The fake provider plays this deterministically so it works with no API key.
