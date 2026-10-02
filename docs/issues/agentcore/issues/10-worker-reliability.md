# 10: Worker reliability

**What to build:** Runs execute exactly once and never get stuck. Duplicate broker deliveries and concurrent deliveries of the same run are harmless; a run whose worker died mid-loop is detected and marked failed with a clear "worker lost" error rather than staying `running` forever or being replayed; brief infrastructure and LLM-provider hiccups are retried with backoff instead of failing the run.

**Blocked by:** 05 (Tracer run)

**Status:** done

- [x] Celery configured with `acks_late` and reject-on-worker-lost
- [x] Heartbeat lease: Redis key with TTL set on claim and refreshed every iteration, cleared on terminal transition
- [x] Redelivered task finding the run `running` with an expired lease → mark failed with an `error` step "worker lost" and publish `done`; with a live lease → no-op; never resumes
- [x] Failures before the claim (e.g. database unreachable) retried by Celery autoretry with backoff
- [x] Planner retries transient provider errors (429, 5xx, timeouts) with exponential backoff and jitter, ~3 attempts
- [x] Tests (runner seam): two concurrent invocations on a queued run execute exactly once; invocation on completed/failed/cancelled runs is a no-op; expired lease → failed "worker lost"; live lease → no-op; planner retries a transient error then succeeds, and gives up after the limit (run fails)
