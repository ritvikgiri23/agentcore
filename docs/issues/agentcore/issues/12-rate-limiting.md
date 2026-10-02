# 12: Active-run rate limiting

**What to build:** Each user can have at most 10 queued or running runs at once; the 11th submission is rejected with a 429 in the standard envelope. Slots free up the moment a run reaches any terminal state, and if the bookkeeping ever drifts the limit heals itself from the database rather than locking the user out.

**Blocked by:** 11 (Run cancellation)

**Status:** done

- [x] Per-user Redis set of active run ids; admission via an atomic Lua script that adds the run id only if the set size is below the limit (configurable, default 10)
- [x] Admission happens before the run is inserted; 429 `rate_limited` envelope when full
- [x] Release (idempotent set removal) called from the lifecycle service on every terminal transition: completed, failed (including worker lost), cancelled, and enqueue failure
- [x] On admission failure, reconcile the set once against Postgres (runs with status queued or running for that user) and retry before returning 429
- [x] Tests (HTTP seam): 11th active run → 429; slot released on completion, failure and cancellation (next submission succeeds); drifted set with stale ids reconciled and submission admitted; double release harmless; two simultaneous submissions at 9 active admit exactly one

## Comments

**Implementation notes.** The per-user "set" is a Redis sorted set scored by admission time (`app/runs/admission.py`). Admission happens before the insert, so a reconcile can see an id that is not in Postgres yet. Ids missing from Postgres are only dropped once they are older than `INSERT_GRACE_SECONDS`, so a run that is about to be inserted never loses its slot. Ids whose runs are terminal are dropped immediately. Reconciliation also adds the user's active runs that the set has lost, so drift can't let a user go over the limit either. Release lives in `lifecycle`: whichever writer wins a terminal transition frees the slot, and if the release fails it is logged and left for reconciliation.
