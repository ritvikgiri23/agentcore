# 0001: Bridge Celery to the async runner with one event loop per task

Status: accepted

## Context

Everything the agent loop touches is async: asyncpg via SQLAlchemy's asyncio layer,
`redis.asyncio`, `AsyncOpenAI`, and async tools. Celery tasks are synchronous functions
in a prefork pool. asyncpg connections, Redis clients and `httpx` clients are bound to
the event loop that created them. Using one from another loop fails at runtime, often
only under load.

The alternatives were:
- a long-lived event loop per worker process, shared by tasks;
- an async-native queue (arq, Dramatiq with asyncio, or a hand-rolled Redis consumer);
- making the runner synchronous.

## Decision

`execute_agent_run(run_id)` calls `asyncio.run(_execute(run_id))`. Each task gets:
- a fresh event loop;
- its own async engine with `NullPool`, so no connection is cached across loops;
- its own Redis client and its own LLM and embedder clients.

All of these are created inside the loop and disposed in `finally` before it closes.
The runner takes them through a `RunnerDeps` value, so the test suite calls the same
`run_agent(run_id, deps)` in-process with its own engine, fakeredis and a scripted
LLM, without a broker.

The pool is prefork (concurrency 4) with `acks_late`, `reject_on_worker_lost` and
`prefetch_multiplier=1`. The task id is the run id, so revocation needs no lookup.

The revoke path follows from this design. `revoke(terminate=True, signal="SIGUSR1")`
makes Celery raise `SoftTimeLimitExceeded` inside the running task. The runner lets
it propagate (it's declared as an interrupt in `RunnerDeps`) rather than failing the
run. The task catches it and settles the cancellation on a second, short
`asyncio.run`, because the first loop is gone by then.

## Consequences

- Each run pays for a new connection to Postgres and Redis, a few milliseconds. That's
  negligible next to LLM latency, and it means no cross-loop sharing bugs.
- A run's database connections are bounded by worker concurrency, not by pool size.
- Each worker process executes one run at a time. Concurrency within a run (parallel
  tool calls) happens inside its loop. Concurrency across runs comes from processes.
- Celery's mature retry, revoke and acks-late semantics are kept, at the cost of the
  sync/async seam living in one small module (`app/worker/tasks.py`).
