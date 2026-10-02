# 12: Active-run rate limiting

**What to build:** Each user can have at most 10 queued or running runs at once; the 11th submission is rejected with a 429 in the standard envelope. Slots free up the moment a run reaches any terminal state, and if the bookkeeping ever drifts the limit heals itself from the database rather than locking the user out.

**Blocked by:** 11 (Run cancellation)

**Status:** ready-for-agent

- [ ] Per-user Redis set of active run ids; admission via an atomic Lua script that adds the run id only if the set size is below the limit (configurable, default 10)
- [ ] Admission happens before the run is inserted; 429 `rate_limited` envelope when full
- [ ] Release (idempotent set removal) called from the lifecycle service on every terminal transition: completed, failed (including worker lost), cancelled, and enqueue failure
- [ ] On admission failure, reconcile the set once against Postgres (runs with status queued or running for that user) and retry before returning 429
- [ ] Tests (HTTP seam): 11th active run → 429; slot released on completion, failure and cancellation (next submission succeeds); drifted set with stale ids reconciled and submission admitted; double release harmless; two simultaneous submissions at 9 active admit exactly one
