# 0003: Execute each run at most once, with an atomic claim and a heartbeat lease

Status: accepted

## Context

With `acks_late` and `reject_on_worker_lost`, Celery redelivers a task if its worker
dies, and brokers may deliver twice anyway. A run's tools have side effects:
`remember_fact` writes memories, and real-world tools would send email or charge
cards. So a run must:
- execute at most once;
- never stay `running` after its worker died;
- never be resumed halfway, which would replay side effects.

A status check followed by a write is racy. A Postgres advisory lock dies with its
connection but says nothing to a different process later. A plain Redis lock with no
fencing lets a stalled worker clobber its successor.

## Decision

**Atomic claim.** `UPDATE agent_runs SET status='running', started_at=clock_timestamp()
WHERE id=:id AND status='queued' RETURNING *`. Exactly one caller gets the row back.
Every terminal transition is conditional the same way:
- `running → completed`
- `queued|running → failed`
- `queued|running → cancelled`

so a terminal status is never overwritten. All of these live in `app/runs/lifecycle.py`
and nowhere else.

**Heartbeat lease.** A Redis key `run:{id}:lease`, `SET NX EX 120`, holding a random
token. The worker takes it *before* claiming and refreshes it at every iteration.
Refresh and release are check-and-set under `WATCH`, so only the token holder can
change it. A redelivered task that finds the run:

| Run status | Lease | Outcome |
|---|---|---|
| `queued` | — | Claims it and executes. |
| `running` | held | `RunInProgress`: re-enqueue a fresh check for when the lease could expire. This doesn't consume the retry budget. |
| `running` | expired | Record an `error` step ("worker lost"), mark it `failed`, publish `done`. Never resume. |
| terminal | — | Do nothing. |

The lease is taken before the claim so that a duplicate can never see a run that is
`running` but not yet leased and mistake it for a lost one.

A worker whose own lease lapsed (it stalled past the TTL) tries to retake it. If
someone else holds it, or the run has already been failed, the worker stops without
writing anything.

**Pre-claim failures** (database not reachable yet) are safe to retry: Celery
`autoretry_for` with jittered backoff, up to 5 times.

## Consequences

- Duplicate deliveries are harmless: no double tool calls, no duplicate memories.
- A crashed run is failed as "worker lost" on redelivery. Detection is bounded by the
  lease TTL (about 2 minutes).
- A run that is `running` with no redelivery at all (the broker lost the message) needs
  a sweeper. That's listed as future work.
- Long single steps must stay well under the TTL, since the lease is refreshed per
  iteration. With a 5 s tool timeout and LLM timeouts in the tens of seconds, 120 s
  leaves headroom.
