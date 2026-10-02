# 0002: SSE streams replay persisted steps, then relay live ones

Status: accepted

## Context

Clients watch a run through `GET /runs/{id}/stream`. They connect at arbitrary times:
before the worker starts, halfway through, or after the run finished. Redis pub/sub is
fire-and-forget, so a subscriber only sees what's published after it subscribes.
Reading Postgres alone would mean polling.

Each step must arrive exactly once and in order, and the stream must end
deterministically.

## Decision

Two invariants and one algorithm.

**Invariant 1: persist, then publish.** The worker commits each step to Postgres
before publishing it (`app/runs/events.py`). Postgres is the source of truth and
pub/sub only the live tail, so any published step can be found in the database.

**Invariant 2: `done` is an event, not a step.** The terminal
`{"step_type": "done", "status": …}` is published after the terminal status is
committed, and never persisted.

**Algorithm** (`app/runs/stream.py`):

1. Check ownership, then **subscribe** to `run:{id}:events`, *before* reading anything.
   From here on, live events buffer in the subscription.
2. Read the run's status, then its persisted steps in `(occurred_at, id)` order.
   Status comes first because once a run is terminal, all its steps are persisted.
   Emit the steps and remember their ids.
3. If the status was terminal, emit `done` and close.
4. Otherwise read the subscription. Skip step ids already sent, which covers steps
   that were both replayed and buffered. Stop at `done`.
5. Before emitting `done`, re-read the steps and send any whose live publish was lost.

If 15 s pass with no events, the stream re-reads the status from Postgres, so a lost
`done` can't hang it. A deleted run counts as cancelled. FastAPI's `EventSourceResponse`
sends `: ping` keepalive comments on the same 15 s cadence. On client disconnect, the
dependency's context manager unsubscribes and closes the pubsub, with nothing logged
as an error.

The token may come from the `Authorization` header or an `access_token` query parameter
(for `EventSource`). The request log scrubs the parameter.

## Consequences

- Late joiners miss nothing, and overlap is deduplicated by step id, a database
  sequence that orders steps sharing an occurrence time.
- Every live event costs a Postgres write first, which is the price of making replay
  trivially correct.
- Every reconnect replays from the start. That's fine for traces of tens of steps.
  Redis Streams with `Last-Event-ID` resumption is the upgrade path (see NOTES,
  future work).
- Short-lived reads commit straight away, so a long-lived stream doesn't pin a pooled
  connection in an open transaction.
