# AgentCore: design notes

How the system works, why it works that way, where it departs from the brief, and
what I'd do next. The README covers how to run it. The five biggest decisions each have
an ADR in [docs/adr/](docs/adr/).

- [The agent loop](#the-agent-loop)
- [Memory strategy](#memory-strategy)
- [Reliability: runs that can't get stuck](#reliability-runs-that-cant-get-stuck)
- [Deviations from the brief](#deviations-from-the-brief)
- [Review questions](#review-questions)
- [Future work](#future-work)

---

## The agent loop

`app/runs/runner.py` · `run_agent(run_id, deps)`

The API never runs the agent. `POST /sessions/{id}/run` takes a rate-limit slot, inserts
a `queued` run, commits, and enqueues a Celery task whose id **is** the run id. The
worker then:

1. **Takes the lease, then claims the run.** It sets a Redis heartbeat lease
   (`SET NX EX 120`), then runs `UPDATE agent_runs SET status='running' WHERE id=:id
   AND status='queued' RETURNING *`. A run is executed only by whoever wins that
   conditional update. Anything else (already terminal, already running, deleted)
   exits without doing work. See [ADR 0003](docs/adr/0003-atomic-claim-and-heartbeat-lease.md).
2. **Builds the context.**
   - **System message:** the session's persona plus up to three long-term memories
     within `MEMORY_MAX_DISTANCE`, in a delimited `<relevant_memories>` block. The block
     carries a "may be outdated; prefer what the user says now" caveat.
   - **Short-term turns:** the session's last five, as alternating user/assistant
     messages.
   - **The new user message.**

   This is recorded as a `memory_retrieval` step, listing the memories with their
   distances and the number of turns used.
3. **Iterates**, up to `MAX_ITERATIONS` (10) times:
   - **Step boundary:** check the cancel flag and refresh the lease.
   - **LLM call:** messages plus the session's tool schemas, with `tool_choice=auto`.
     Recorded as an `llm_call` step with model, finish reason, content, requested tool
     calls, usage and latency, but not the prompt (it would make every trace quadratic).
     `tokens_used` is updated on the run row after every call.
   - **No tool calls:** that's the answer. Record `final_answer`, complete the run, push
     the turn to short-term memory, publish `done`.
   - **Tool calls:** record a `tool_call` step for each, in order. Dispatch them all
     concurrently with `asyncio.gather`, then record each `tool_result` (truncated to
     `TOOL_RESULT_MAX_CHARS` in the trace; the model sees the full result) or
     `tool_timeout`. Append the tool messages in the original call order, one per tool
     call id, error or not, as the OpenAI protocol requires.
4. **On the cap:** `final_answer` = "Max iterations reached" with `max_iterations_hit:
   true`. The run is `completed`, because it ended as designed rather than crashed, but
   the turn is **not** written to short-term memory, so a confused run can't poison
   the next one.

**How the loop stops**

| Condition | Steps | Status | Short-term turn |
|---|---|---|---|
| Plain answer | `final_answer` | completed | written |
| 10-iteration cap | `final_answer` ("Max iterations reached", `max_iterations_hit`) | completed | not written |
| Cancel flag seen at a step boundary | `cancelled` | cancelled | not written |
| Task revoked while blocked in a call | `cancelled` (settled by the task's handler) | cancelled | not written |
| Unhandled exception | `error` (type + message; traceback only in logs) | failed | not written |
| Worker died (lease expired, redelivered) | `error` "worker lost" | failed | not written |
| Run deleted with its session | none; stops quietly | (row gone) | not written |

A step boundary sits before every LLM call, before tool dispatch and before the final
answer. Cancellation is therefore cooperative at the next boundary, and forcible (via
revoke) when the loop is stuck inside a slow call.

**Tool dispatch never raises** (`app/tools/dispatcher.py`). Each of these becomes an
error *result* that goes back to the model, so it can correct itself:

- an unknown tool
- a tool that isn't enabled for the session
- invalid JSON
- a non-object argument
- arguments that fail schema validation
- an exception inside the tool
- a timeout (5 s)

Unknown and disabled tools also list the tools that are available. One bad tool call
can't crash the loop. Sync tools run in a worker thread, so a stuck one is abandoned
after the timeout (Python threads can't be killed). Async tools are awaited under
`asyncio.timeout`.

**Persist, then publish.** Every step is committed to Postgres *before* it is published
to the run's Redis channel (`app/runs/events.py`). Postgres is the source of truth and
pub/sub only the live tail. Any event a subscriber sees can therefore be found in the
database, and the SSE replay-then-live algorithm relies on that. See
[ADR 0002](docs/adr/0002-sse-replay-then-live.md).

**Transient LLM errors** (429, 5xx, timeouts) are retried by the SDK and then by a
small wrapper: 3 attempts, exponential backoff with full jitter, capped at 8 s.
Anything else, such as a 400, would fail the same way again, so it fails the run.
An embedding failure at retrieval is logged, and the run proceeds without memories.

## Memory strategy

### Short-term: what the agent was just talking about

`app/memory/short_term.py`. One Redis list per session, `session:{id}:history`, newest
first.

- **Write.** Only **completed runs with a real answer** are written. Failed, cancelled
  and max-iteration runs are left out, because a broken turn in the context would make
  the next one worse. The write `LPUSH`es `{user, assistant, run_id, ts}` and
  `LTRIM`s to 20 in one transaction.
- **Read.** `LRANGE 0 4` gives the last 5 turns (10 messages), reversed into
  chronological order.
- **Window vs retention.** The context window is 5 turns, but 20 are retained. The
  window is what the LLM pays for on every call. Retention is cheap and gives headroom
  for a later summarisation step or a larger window, without the list growing
  unbounded.
- **Eviction** is the `LTRIM`: the oldest turn falls off as the 21st arrives. Deleting
  the session deletes the key.
- **Ordering.** The turn is pushed *before* `done` is published, so a client that sends
  a follow-up the moment it sees `done` already has that turn in its context.

Turns are *not* in Postgres. They're a rolling cache of context, not a record, and the
run table already keeps every message and answer durably.

### Long-term: what the agent knows about you

`app/memory/long_term.py`, `app/tools/memory.py`. The `long_term_memory` table holds
`(user_id, content, embedding vector(1536), source_run_id, created_at)`.

- **Writes are explicit.** Only the `remember_fact` tool writes, when the model decides
  a fact is durable. Nothing is auto-extracted from every turn: that would fill the
  table with noise and make retrieval worse.
- **Retrieval.** At run start, the user's message is embedded and the top 3 memories by
  cosine distance (`<=>`) are fetched for **that user only**. Anything beyond
  `MEMORY_MAX_DISTANCE` is dropped. The default of 1.0 is permissive: it only drops
  memories that are clearly unrelated. The step records each memory's distance, so you
  can see why the agent "knew" something.
- **Dedup.** `remember_fact` first finds the user's single nearest memory. If it's
  within `MEMORY_DEDUP_DISTANCE` (0.05, effectively the same sentence), the tool
  returns `Already remembered: …` and stores nothing. Without this, asking the same
  question five times would leave five copies, all crowding the top-3 slots.
- **Lifetime.** Memories belong to the **user**, not the session. They survive session
  deletion: `source_run_id` is `ON DELETE SET NULL`, so provenance is lost but the
  fact is kept. Users list, search and delete them through `/memory`. There is no TTL.
  Facts are curated by the model and deleted by the user, and the recall prompt tells
  the model that a memory may be outdated.
- **Search vs recall.** `GET /memory/search` applies no distance cutoff and returns 5
  results, so it shows what *could* be recalled. The run's recall uses the cutoff.

### pgvector indexes

There are two indexes: a B-tree on `user_id`, and an HNSW index on `embedding` with
`vector_cosine_ops`. The operator class must match the `<=>` operator, or the index is
silently unused. The cost analysis is under [review question 4](#4-what-does-pgvector-cost-and-how-is-it-indexed).

## Reliability: runs that can't get stuck

**Lifecycle.** Status only changes in `app/runs/lifecycle.py`, and every transition is
a conditional `UPDATE … WHERE status IN (…)`:

```text
            claim                 complete
 queued ─────────────► running ─────────────► completed
   │                     │  │
   │ cancel              │  │ fail (exception, worker lost)
   ▼                     │  ▼
 cancelled ◄─────────────┘  failed
            cancel
```

A terminal status is never overwritten, so a worker finishing up can't turn
`cancelled` back into `completed`. Whichever writer wins a terminal transition releases
the user's rate-limit slot, exactly once.

**Redelivery.** Celery runs with `acks_late` and `reject_on_worker_lost`, so a killed
worker's message is requeued. The redelivered task finds one of three things:

- the run is **terminal**: it does nothing;
- the run is `running` and **leased by someone alive**: it re-checks once the lease
  could have expired;
- the run is `running` with **no lease**: it is failed as "worker lost".

Runs are **failed, never resumed**. Tools have side effects (`remember_fact` writes),
and replaying half a run would repeat them.

**Pre-claim failures** (database not reachable yet, Redis blip) escape the runner
before anything has happened. Celery `autoretry_for` retries them with jittered backoff.

**Cancellation.**

1. `DELETE /runs/{id}` atomically transitions the run to `cancelled`.
2. It sets a Redis cancel flag holding the reason, and releases the slot.
3. It revokes the task with `terminate=True, signal=SIGUSR1`.
4. A queued run is dropped before it starts.
5. A running loop sees the flag at its next step boundary.
6. A loop blocked in a call gets `SoftTimeLimitExceeded` from the signal. The task
   then settles the run on a fresh event loop: one `cancelled` step, then `done`.

Settling is idempotent under a row lock, so the API and the worker racing to settle
produce exactly one `cancelled` step.

## Deviations from the brief

Each one is deliberate. The brief is right about intent in every case; these are places
where following its letter would have cost correctness or clarity.

| # | Brief | AgentCore | Why |
|---|---|---|---|
| 1 | Endpoint table: login returns "access + refresh" tokens | **Access token only** (30 min) | The brief's own feature section only describes access tokens with a 30-minute expiry. A refresh token without rotation, revocation and storage is a long-lived bearer secret with none of the safety, and building those properly is its own feature. Listed as future work. |
| 2 | Calculator via `ast.literal_eval` | **Whitelisted AST walker** | `literal_eval` doesn't evaluate operators: `literal_eval("2*3")` raises. The walker keeps the same principle (parse, never `eval`), allows only numeric constants, `+ - * / // % **` and unary minus, and rejects names, calls, attributes and everything else. It also guards what `literal_eval` never needed to: exponent and result-size caps (`9**9**9` is rejected before it's computed), an input-length cap, and division by zero as an error string. |
| 3 | "All tools are synchronous" | `remember_fact` and `summarise_text` are **async**; the other three are sync | Both do I/O on the worker's event loop: an embedding call plus a Postgres query, or an LLM call. Running them in a thread would need a second event loop per call, because asyncpg connections and the OpenAI client are bound to the loop that created them. The registry supports both: sync tools run via `asyncio.to_thread`, async tools are awaited, and both get the same 5 s timeout. |
| 4 | Relationships `lazy="selectin"` | **`lazy="raise"`** | `selectin` loads every run of a session, and every step of each run, whenever a session is read. A session with 1,000 runs makes `GET /sessions` load the entire trace history. With `raise`, an accidental lazy load is a loud error. Every query that needs related rows says so explicitly, e.g. the session detail's limited "last 5 runs" query. |
| 5 | Step types | **Eight types**: `memory_retrieval`, `llm_call`, `tool_call`, `tool_result`, `tool_timeout`, `final_answer`, `error`, `cancelled` (enforced by a CHECK constraint) | The trace should explain *every* outcome. Without `memory_retrieval` you can't see why the agent knew something. Without `tool_timeout`, `error` and `cancelled`, a timed-out, failed or cancelled run would end its trace with no explanation. The stream's `done` event is deliberately *not* a step and is never persisted. |
| 6 | `agent_runs` columns | **Adds `started_at`, `finished_at`** | Queue latency (`started_at − created_at`) and execution time (`finished_at − started_at`) are the first two things you need when a run is slow, and the status endpoint returns both. They're set by the same conditional updates that change status, so they can't disagree with it. |
| 7 | Paths like `/sessions` | **Everything under `/api/v1`** (the `/demo` page excepted) | A versioned prefix from day one costs nothing. Adding it later breaks every client. The README maps each brief path to its `/api/v1` equivalent. |
| 8 | Rate limit as a per-user "counter" | **A per-user Redis sorted set of active run ids**, admitted by a Lua script | A counter drifts: a crash between `INCR` and `DECR` leaks a slot forever, and a double release goes negative. A set makes release idempotent (`ZREM`) and lets the server reconcile against Postgres, because it knows *which* runs it's counting. The cardinality is the count. It's a sorted set, scored by admission time, so a run admitted milliseconds ago but not yet inserted isn't mistaken for stale during reconciliation. See [ADR 0005](docs/adr/0005-rate-limit-set-and-lua-admission.md). |

Smaller choices in the same spirit:
- Another user's resource is a **404, not 403**, so its existence never leaks.
- `DELETE /runs/{id}` returns **200 with the new status**, rather than a bare 202, since
  the transition has already happened by the time it responds.
- There is no session `PATCH`, because the brief's endpoint table has none.

## Review questions

### 1. How does the agent loop stop?

The loop itself stops in three ways:

- **Plain answer.** An LLM response with no tool calls *is* the answer. Record
  `final_answer`, complete the run.
- **Iteration cap.** After 10 LLM calls the run completes with "Max iterations
  reached" and `max_iterations_hit: true`. The cap bounds tokens and time for a model
  that keeps calling tools. The run is completed, not failed, because it ended as
  designed, but the turn isn't saved to short-term memory.
- **Cancel flag.** This is checked at every step boundary (before each LLM call,
  before tool dispatch and before the final answer). It records `cancelled`.

The run can also end without the loop choosing to:
- **Revocation** (SIGUSR1 → `SoftTimeLimitExceeded`) interrupts a loop blocked inside
  a call.
- **Unhandled exceptions** become an `error` step and status `failed`.

In every case the stream gets `done` with the terminal status. The full table is in
[The agent loop](#the-agent-loop).

### 2. What happens when the LLM hallucinates a tool?

The dispatcher returns an **error result** rather than raising: `Unknown tool
'get_weather'. Available tools: calculator, remember_fact, web_search`. That goes back
to the model as the tool message for that call id, and the loop continues. In practice
the model picks a real tool on the next iteration. The trace shows a `tool_result` with
`is_error: true`.

The same path handles two other cases. A tool that exists but **isn't enabled** for the
session returns "not enabled for this session". **Malformed or schema-invalid
arguments** come back with the validation messages.

Enablement is enforced **at dispatch time**, not only by which schemas are sent to the
model. A prompt-injected instruction to "call `summarise_text`" can't reach a tool the
session didn't enable, even if the model emits the call. Likewise, a tool's
`ToolContext` (user id, run id, DB, LLM) is injected server-side and isn't in the
schema, so the model can't choose which user a tool acts on.

### 3. Trace a run from the POST to the SSE stream closing

1. **Admission.** The API takes a slot in the user's active-run set with an atomic Lua
   script (**Redis**).
2. **Insert.** It inserts the run as `queued` and **commits before enqueueing**
   (**Postgres**), so the worker can always find it. If enqueueing fails, the run is
   marked failed and its slot released.
3. **Enqueue.** It enqueues with `task_id = run_id` (**Redis** as the broker) and
   returns 202.
4. **Subscribe and replay.** The stream checks ownership, **subscribes first**, then
   replays persisted steps from **Postgres**. Anything published after the subscribe is
   buffered, so the gap between replay and live is covered. Duplicates are dropped by
   step id.
5. **Claim.** The worker takes the lease (**Redis**) and atomically claims the run
   (**Postgres**).
6. **Execute.** For each step: commit to **Postgres**, then publish to **Redis**. The
   stream relays it.
7. **Finish.** The worker writes the terminal status (**Postgres**, which also frees
   the slot in **Redis**) and pushes the turn to short-term memory (**Redis**), then
   publishes `done`.
8. **Close.** The stream emits `done` with the terminal status and closes. Every
   15 s of silence brings a keepalive comment and a status re-check, so a lost `done`
   can't hang the stream. A client disconnect unsubscribes quietly.

### 4. What does pgvector cost, and how is it indexed?

At the brief's scale, about **500 memories**, it costs almost nothing. The query is
always `WHERE user_id = :me ORDER BY embedding <=> :q LIMIT 3`. Postgres uses the
**B-tree on `user_id`** to fetch that user's rows, then exact-sorts them by distance.
Measured on the compose Postgres (pgvector 0.8.7) with 500 random 1536-dim vectors over
10 users: a bitmap index scan, a top-N heapsort, **0.19 ms**. The search is exact, so
there's no recall loss. Storage is about 6 KB per row (1536 × 4 bytes), so 500 rows is
about 3 MB.

The **HNSW** index (`vector_cosine_ops`, matching `<=>`) is there for when the table
outgrows exact scans. But HNSW has a well-known trap with **filtered ANN**. If the
planner walks the HNSW graph and then applies `WHERE user_id = :me`, it finds the
nearest `ef_search` (40 by default) candidates across **all** users and filters
afterwards. A user who owns 0.1% of the rows may get **fewer than k results, or
none**, even though they have relevant memories.

The mitigations, in the order I'd reach for them:
- **Keep per-user candidate sets small.** The planner then prefers the B-tree plus an
  exact sort. This holds while any one user has thousands of memories, not millions,
  which is the realistic shape for "facts about a user".
- **Iterative index scans.** pgvector 0.8's `SET hnsw.iterative_scan = relaxed_order`
  keeps walking the graph until enough rows pass the filter. It's available in the
  bundled version but not enabled yet.
- **Partition by user** (or by user hash), or use partial indexes for very large
  tenants, so each graph only contains rows that can match.
- Raise `hnsw.ef_search` for recall at some latency cost.

### 5. Concurrency and isolation

**Between workers and requests**

| Hazard | Guard |
|---|---|
| Broker delivers a run twice | Lease (`SET NX`) taken before the claim, plus the claim itself (`UPDATE … WHERE status='queued' RETURNING`). Exactly one executes; the other no-ops or re-checks later. |
| Cancel races completion | Conditional terminal transitions. `cancel` locks the row it reads; `complete` only moves `running → completed`. Whoever commits first wins, and the loser sees `False` and backs off. |
| Two settles of one cancelled run | `SELECT … FOR UPDATE` plus an "already has a `cancelled` step" check. Exactly one step is recorded. |
| Two submissions racing for the 10th slot | Admission is one Lua script: `ZCARD` and `ZADD` run atomically on the Redis server. |
| Slot leaked by a crash | Reconcile the set against Postgres before refusing; stale ids are dropped. |
| A worker stalled past its lease TTL | The lease is held by token. A stalled worker can't refresh or release a lease someone else has taken, and it stops if the run has been failed as worker lost. |
| asyncpg connections bound to an event loop | Each Celery task runs `asyncio.run` with its own engine (`NullPool`) and Redis client, both disposed afterwards. Nothing crosses loops. |

**Between users**

- **Ownership is part of every query.** Lookups go through
  `app/repositories/ownership.py`, which filters by the caller's id *in SQL*. Runs and
  steps are filtered via a join to the session's `user_id`. A non-owned resource and a
  missing one are the same 404.
- **Memory is scoped in the query layer.** Every long-term memory query takes
  `user_id`, including the dedup check inside `remember_fact`.
- **The model can't pick the user.** Tools get the user from a server-injected
  `ToolContext` that isn't part of the LLM-facing schema.
- **The stream checks ownership before subscribing.** Once an SSE response starts,
  it's already a 200, so the check has to happen first.
- **The tool allowlist is enforced at dispatch**, not only by which schemas are sent
  to the model.

## Future work

- **Stream tickets.** The SSE `access_token` query parameter is scrubbed from request
  logs, but URLs still leak into browser history and proxy logs. The production
  upgrade is `POST /runs/{id}/stream-ticket`, returning a single-use ticket that lives
  30 seconds and is redeemed by the `EventSource` URL. The `Authorization` header keeps
  working for non-browser clients.
- **A real search provider.** `web_search` is a deterministic fixture. Swapping in
  Tavily or SerpAPI means changing one function: an `async` tool with an HTTP client,
  plus caching and per-user quotas.
- **Redis Streams for events.** Pub/sub is fire-and-forget. Correctness comes from
  Postgres replay, but a subscriber that falls behind relies on that replay. With
  `XADD`/`XREAD` and a per-run stream with a short TTL, a reconnect could resume from a
  `Last-Event-ID` without touching Postgres, and fan-out to many viewers would be
  cheaper.
- **Iterative HNSW scans or partitioning** for long-term memory once tenants are large.
  See [review question 4](#4-what-does-pgvector-cost-and-how-is-it-indexed).
- **Refresh tokens** with rotation, reuse detection and server-side revocation, plus
  logout.
- **Context management.** The 20 retained turns allow summarising older turns into a
  running synopsis instead of dropping them. Token-aware truncation would keep a run
  within the model's context window.
- **Operations.** OpenTelemetry tracing across API → broker → worker (a request id
  already flows into logs), Prometheus metrics for queue depth and run latency, and a
  periodic sweeper that fails `running` runs whose lease expired with no redelivery.
