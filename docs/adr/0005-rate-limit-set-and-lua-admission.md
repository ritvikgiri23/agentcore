# 0005: Limit active runs with a per-user Redis sorted set and Lua admission

Status: accepted

## Context

Each user may have at most 10 runs that are queued or running. The check runs on every
submission, so it should be one Redis round trip. It has to stay correct when:
- two submissions race for the last slot;
- a worker crashes before releasing a slot;
- a release happens twice;
- a run finishes between the check and the insert.

An `INCR`/`DECR` counter fails several of these. A crash between them leaks a slot
forever. A double release drives it negative. And because a number doesn't say which
runs it counts, it can't be repaired. A `SELECT count(*)` on Postgres per submission is
correct, but it races unless it takes a lock, and it puts load on the primary database.

## Decision

`app/runs/admission.py` keeps one sorted set per user, `user:{id}:active_runs`. Its
members are run ids, scored by admission time.

- **Admit** is an atomic Lua script: `if ZCARD < limit then ZADD run_id; return 1 end`.
  It runs *before* the run is inserted, using the run id the API has just generated.
- **Release** is `ZREM`, idempotent, called by whichever writer wins a terminal
  transition in `app/runs/lifecycle.py`. If insert or enqueue fails, it's released
  too.
- **Reconcile.** When admission fails, the set is compared once against Postgres (the
  user's runs that are queued or running, plus every id in the set):
  - Members that are terminal or missing are dropped.
  - Active runs that are missing from the set are added back.
  - Admission is then retried. Only if it still fails does the user get a **429**,
    with `details.limit`.
- **Grace window.** The score exists so reconciliation doesn't drop a run admitted in
  the last 60 s that simply hasn't been inserted yet.

## Consequences

- The common path costs one Redis round trip, and two submissions can't both take the
  10th slot.
- Drift from crashes heals itself the next time the user is at the limit. The user is
  never locked out by stale ids.
- Release is safe to call more than once, so every terminal path can call it without
  coordination.
- Postgres stays the source of truth. Redis holds a cache of "which runs are active"
  that can always be rebuilt.
- The limit is soft by up to the grace window if an insert fails without its release
  running. That's acceptable, and reconciliation fixes it once the window passes.
