# AgentCore

A backend for tool-using AI agents. You define an **agent session** (a persona: name,
system prompt, enabled tools), submit a message to it, and get a `run_id` back at once.
A Celery worker runs a ReAct-style loop (LLM → tools → LLM → … → answer), persisting
every **run step** to Postgres and publishing it live to Redis. You can poll the run,
read its full trace, or watch it think over Server-Sent Events. The agent carries recent
conversation turns as short-term memory and stores durable facts in pgvector long-term
memory.

It runs with **no API key**. Without `OPENAI_API_KEY`, a deterministic fake LLM and
embedder play the flagship scenario end to end.

- **Stack:** FastAPI · SQLAlchemy 2 (async) + asyncpg · PostgreSQL 16 + pgvector ·
  Redis 7 · Celery 5 · structlog · uv
- **Docs in this repo:** [NOTES.md](NOTES.md) covers design, memory strategy,
  deviations from the brief, review answers and future work. [docs/adr/](docs/adr/)
  records the five biggest decisions. [CONTEXT.md](CONTEXT.md) is the glossary.

## Quickstart

```sh
cp .env.example .env
docker compose up --build
```

Compose starts Postgres (with pgvector) and Redis, runs the Alembic migration once,
then starts the API and a Celery worker (prefork, concurrency 4) after the migration
succeeds. Once it's up:

| | |
|---|---|
| Swagger UI | <http://localhost:8000/docs> |
| Browser demo | <http://localhost:8000/demo> |
| Health | <http://localhost:8000/api/v1/health> |

If port 8000 is taken, set `API_PORT` in `.env`.

### Make targets

| Command | What it does |
|---|---|
| `make up` | Build and start the production-like stack, waiting until healthy |
| `make up-dev` | The same with source mounted, API auto-reload and worker restart on change; publishes Postgres/Redis on the host |
| `make down` | Stop the stack |
| `make logs` | Tail API and worker logs (structured JSON) |
| `make migrate` | Apply migrations |
| `make test` | Run the test suite with a coverage report (see [Tests](#tests)) |
| `make demo` | Play the flagship scenario with curl against the running stack |

## Fake mode vs OpenAI mode

| | Fake (default) | OpenAI |
|---|---|---|
| Enabled when | `OPENAI_API_KEY` is empty | `OPENAI_API_KEY` is set (or `LLM_PROVIDER=openai`) |
| Chat model | `DemoLLM`: plays the Dubai scenario and gives a canned hint for anything else | `gpt-4o-mini` (`CHAT_MODEL`) |
| Embeddings | Hash-based, deterministic, 1536-dim | `text-embedding-3-small` (`EMBEDDING_MODEL`) |
| Cost | Zero | Real tokens; `tokens_used` on every run |

The fake model is not a script that ignores its inputs. It reads the conversation so far
and acts on real tool outputs: the population it multiplies is the one `web_search`
returned, and the answer quotes what `calculator` computed. Every tool, step, stream,
memory and rate-limit path therefore runs for real in fake mode too.

To switch, put the key in `.env` and restart: `docker compose up -d --force-recreate api worker`.
Every other setting is documented in [`.env.example`](.env.example).

## API

Every route sits under `/api/v1`, apart from the demo page. Each path in the brief maps
to its versioned equivalent:

| Brief | AgentCore | Notes |
|---|---|---|
| `POST /auth/register` | `POST /api/v1/auth/register` | 201 → `{access_token, token_type, expires_in}`; 409 on a taken email |
| `POST /auth/token` | `POST /api/v1/auth/token` | OAuth2 password form (`username`, `password`); access token only |
| `GET /sessions` | `GET /api/v1/sessions` | `?limit=` (default 50, max 200) `&offset=` → `{items, total, limit, offset}` |
| `POST /sessions` | `POST /api/v1/sessions` | 201; 422 `unknown_tool` lists unknown and available tools |
| `GET /sessions/{id}` | `GET /api/v1/sessions/{id}` | Includes the five most recent run summaries |
| `DELETE /sessions/{id}` | `DELETE /api/v1/sessions/{id}` | 204; cancels active runs, deletes runs and steps and short-term history, keeps long-term memories |
| `POST /sessions/{id}/run` | `POST /api/v1/sessions/{id}/run` | 202 → `{run_id, status, status_url, stream_url}`; 429 at 10 active runs |
| `GET /runs/{id}/status` | `GET /api/v1/runs/{id}/status` | Status, tokens, step count, timestamps, final answer once completed |
| `GET /runs/{id}/steps` | `GET /api/v1/runs/{id}/steps` | Paginated trace in occurrence order |
| `GET /runs/{id}/stream` | `GET /api/v1/runs/{id}/stream` | SSE: replays persisted steps, relays live ones, then `done`. Bearer header or `?access_token=` |
| `DELETE /runs/{id}` | `DELETE /api/v1/runs/{id}` | 200 with the new status (`cancelled`); 409 if already finished |
| `GET /memory` | `GET /api/v1/memory` | Paginated long-term memories |
| `GET /memory/search?q=` | `GET /api/v1/memory/search?q=` | Top 5 by cosine distance, nearest first |
| `DELETE /memory/{id}` | `DELETE /api/v1/memory/{id}` | 204 |
| — | `GET /api/v1/health` | Liveness |
| — | `GET /demo` | Browser demo |

Another user's session, run or memory always returns **404**, never 403, so you can't
even learn that it exists.

Every error uses one envelope, with the request id echoed in `X-Request-ID`:

```json
{"error": {"code": "unknown_tool", "message": "Unknown tools: teleport",
           "details": {"unknown": ["teleport"], "available": ["calculator", "get_current_datetime", "remember_fact", "summarise_text", "web_search"]},
           "request_id": "b63e8be85885424495985e23b9069163"}}
```

The codes the routes return are `validation_error`, `unknown_tool`, `unauthorized`,
`not_found`, `conflict`, `rate_limited`, `service_unavailable` and `internal_error`.
Framework-level errors map to `bad_request`, `forbidden` or `method_not_allowed`, and
any other status falls back to `http_error`. A 500 never carries internal details.

### Tools

| Tool | What it does |
|---|---|
| `web_search(query)` | Three `{title, url, snippet}` results from a deterministic fixture (Dubai population, weather, Python, …; generic fallback otherwise) |
| `calculator(expression)` | Safe arithmetic through a whitelisted AST walker: numbers, `+ - * / // % **`, parentheses, unary minus. Caps on exponent and result size |
| `get_current_datetime()` | Current UTC timestamp, ISO 8601 |
| `summarise_text(text, max_words=100)` | LLM summary, hard-capped at the word limit; its tokens count toward the run |
| `remember_fact(fact)` | Embeds and stores a fact in your long-term memory, skipping near-duplicates |

To add a tool, write one decorated, type-hinted function with a docstring. See
[ADR 0004](docs/adr/0004-tool-registry.md).

## Walkthrough: the Dubai scenario

Sign up, create a research assistant, ask it something that needs search, arithmetic
and memory, then watch it work. Fake mode produces exactly the output shown here. The
commands use `jq` to pull out ids; `make demo` does the same with nothing but `curl`.

```sh
API=http://localhost:8000/api/v1
```

**1. Register.** You get an access token straight away, valid for 30 minutes.

```sh
TOKEN=$(curl -s -X POST $API/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email": "ada@example.com", "password": "correct-horse"}' | jq -r .access_token)
```

```json
{"access_token": "eyJhbGciOiJIUzI1NiIs…", "token_type": "bearer", "expires_in": 1800}
```

Already registered? Log in with the OAuth2 password form instead:

```sh
TOKEN=$(curl -s -X POST $API/auth/token \
  -d 'username=ada@example.com&password=correct-horse' | jq -r .access_token)
```

**2. Create a session** with the three tools the scenario needs.

```sh
SESSION=$(curl -s -X POST $API/sessions \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name": "Research Assistant",
       "system_prompt": "You are a careful research assistant.",
       "tools_enabled": ["web_search", "calculator", "remember_fact"]}' | jq -r .id)
```

```json
{"id": "6e47f661-…", "name": "Research Assistant", "system_prompt": "You are a careful research assistant.",
 "tools_enabled": ["web_search", "calculator", "remember_fact"], "created_at": "2026-10-02T12:30:28.189893Z"}
```

**3. Submit a run.** The response is a 202 and comes back at once: the agent works in
the background.

```sh
RUN=$(curl -s -X POST $API/sessions/$SESSION/run \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message": "What is 15% of the current population of Dubai, and remember that fact for me?"}' \
  | jq -r .run_id)
```

```json
{"run_id": "bb8af742-…", "status": "queued",
 "status_url": "/api/v1/runs/bb8af742-…/status", "stream_url": "/api/v1/runs/bb8af742-…/stream"}
```

**4. Stream it.** `-N` turns off curl's buffering. A stream opened late, even after the
run has finished, first replays every step, so nothing is missed. Each step arrives
exactly once, followed by `done` and the end of the stream.

```sh
curl -N "$API/runs/$RUN/stream?access_token=$TOKEN"
```

```text
data: {"id": 27, "step_type": "memory_retrieval", "payload": {"memories": [], "short_term_turns": 0}, …}
data: {"id": 28, "step_type": "llm_call", "payload": {"iteration": 1, "finish_reason": "tool_calls", "tool_calls": [{"name": "web_search", "arguments": "{\"query\": \"current population of Dubai\"}", …}], "usage": {…}, …}, …}
data: {"id": 29, "step_type": "tool_call", "payload": {"name": "web_search", …}, …}
data: {"id": 30, "step_type": "tool_result", "payload": {"name": "web_search", "result": "[{\"title\": \"Dubai Population 2025 - Dubai Statistics Center\", … 3,655,000 residents …}, …]", "is_error": false, …}, …}
data: {"id": 31, "step_type": "llm_call", "payload": {"iteration": 2, "tool_calls": [{"name": "calculator", "arguments": "{\"expression\": \"3655000 * 15 / 100\"}", …}], …}, …}
data: {"id": 32, "step_type": "tool_call", …}
data: {"id": 33, "step_type": "tool_result", "payload": {"name": "calculator", "result": "548250", …}, …}
data: {"id": 34, "step_type": "llm_call", "payload": {"iteration": 3, "tool_calls": [{"name": "remember_fact", "arguments": "{\"fact\": \"15% of Dubai's population (3,655,000) is 548,250.\"}", …}], …}, …}
data: {"id": 35, "step_type": "tool_call", …}
data: {"id": 36, "step_type": "tool_result", "payload": {"name": "remember_fact", "result": "Remembered: 15% of Dubai's population (3,655,000) is 548,250.", …}, …}
data: {"id": 37, "step_type": "llm_call", "payload": {"iteration": 4, "finish_reason": "stop", "content": "Dubai's current population is about 3,655,000 …", …}, …}
data: {"id": 38, "step_type": "final_answer", "payload": {"content": "Dubai's current population is about 3,655,000 (Dubai Population 2025 - Dubai Statistics Center), so 15% of it is 548,250. I've saved that to your long-term memory.", "iterations": 4, "max_iterations_hit": false}, …}
data: {"step_type": "done", "status": "completed"}
```

The query-string token exists for browsers, whose `EventSource` can't send headers.
`-H "Authorization: Bearer $TOKEN"` works too.

**5. Poll the status.** One call is all a polling client needs.

```sh
curl -s $API/runs/$RUN/status -H "Authorization: Bearer $TOKEN"
```

```json
{"run_id": "bb8af742-…", "status": "completed", "tokens_used": 2526, "step_count": 12,
 "created_at": "2026-10-02T12:30:28.218971Z", "started_at": "2026-10-02T12:30:28.250141Z",
 "finished_at": "2026-10-02T12:30:28.637856Z",
 "final_answer": "Dubai's current population is about 3,655,000 (Dubai Population 2025 - Dubai Statistics Center), so 15% of it is 548,250. I've saved that to your long-term memory."}
```

**6. Read the trace.** These are the same steps the stream delivered, paginated.

```sh
curl -s "$API/runs/$RUN/steps?limit=50" -H "Authorization: Bearer $TOKEN" | jq '.items[] | .step_type'
```

```text
"memory_retrieval"
"llm_call"
"tool_call"
"tool_result"
"llm_call"
"tool_call"
"tool_result"
"llm_call"
"tool_call"
"tool_result"
"llm_call"
"final_answer"
```

**7. Search long-term memory.** The fact outlives the session and is recalled in later
runs.

```sh
curl -s "$API/memory/search?q=Dubai%20population" -H "Authorization: Bearer $TOKEN"
```

```json
[{"id": "73b5b485-…", "content": "15% of Dubai's population (3,655,000) is 548,250.",
  "source_run_id": "bb8af742-…", "created_at": "2026-10-02T12:30:28.542395Z", "distance": 0.5736}]
```

Ask the same question again in the same session. The new run's `memory_retrieval` step
shows the recalled memory and `short_term_turns: 1`, and `remember_fact` answers
`Already remembered: …` instead of storing a duplicate.

**Other things to try**

```sh
# A tool that doesn't exist → 422 unknown_tool, listing the available ones
curl -s -X POST $API/sessions -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name": "x", "system_prompt": "", "tools_enabled": ["teleport"]}'

# Cancel a finished run → 409 conflict
curl -s -X DELETE $API/runs/$RUN -H "Authorization: Bearer $TOKEN"

# Forget a memory → 204
curl -s -X DELETE $API/memory/<memory-id> -H "Authorization: Bearer $TOKEN" -w '%{http_code}\n'

# Delete the session → 204; runs and steps go with it, memories stay
curl -s -X DELETE $API/sessions/$SESSION -H "Authorization: Bearer $TOKEN" -w '%{http_code}\n'
```

## Demo page

Open <http://localhost:8000/demo>. The page registers (or logs in), creates the
Research Assistant session, submits the Dubai prompt and renders each step as it
streams in, using the browser's native `EventSource`. It's one static HTML file served
by the API, with no build step.

## Tests

```sh
make test                                  # whole suite + coverage report (fails under 75%)
make test ARGS="tests/test_runner.py -q"   # one file
make test ARGS="-k cancel --no-cov"        # a subset, without coverage
```

The suite runs in the dev image against the compose Postgres, in a dedicated
`agentcore_test` database that the Postgres init script creates. Each test runs inside a
transaction that is rolled back. Redis is `fakeredis`, which covers lists, sorted sets,
pub/sub and Lua. No test calls OpenAI or needs a broker: runs execute in-process through
the same `run_agent` entry point the Celery task uses. Current coverage is about **96%**
over 290 tests.

The suite is organised around three seams. Each test asserts external behaviour (HTTP
responses, persisted steps, SSE frames, tool outcomes), never internal calls.

1. **HTTP API**, the primary seam: auth, sessions, runs, the stream, memory, rate
   limits, cancellation, cross-user isolation and the error envelope.
2. **The agent runner** (`run_agent(run_id)` with a scripted LLM): stop conditions,
   hallucinated and disabled tools, bad arguments, parallel tools, timeouts,
   cancellation, idempotency, worker-lost and persist-before-publish.
3. **The tool dispatcher**: schema generation, calculator safety, sync and async tools,
   memory dedup.

To run outside Docker: `make up-dev` publishes Postgres and Redis on the host, then
`uv sync && uv run pytest --cov`. Type-check with `uv run mypy` (strict).

## Project layout

```text
app/
  api/v1/        routers: auth, sessions, runs (incl. SSE), memory, health
  api/deps.py    auth, pagination, broker and embedder dependencies
  core/          settings, security, error envelope, logging, request ids, OpenAPI
  models/        users, agent_sessions, agent_runs, run_steps, long_term_memory
  repositories/  owned-resource lookups (the 404-not-403 rule lives here)
  runs/          runner (the loop), lifecycle (status transitions), lease, admission
                 (rate limit), cancellation, events (persist-then-publish), stream
  tools/         registry + dispatcher and the five tools
  memory/        short-term (Redis list) and long-term (pgvector)
  llm/           provider protocol, OpenAI provider + retry, fake and demo models
  worker/        Celery app and the execute_agent_run task
  demo/          the /demo page
alembic/         migrations
docker/          Postgres init script, demo.sh
docs/adr/        architecture decision records
tests/           pytest suite
```
