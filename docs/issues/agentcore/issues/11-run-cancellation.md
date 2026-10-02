# 11: Run cancellation

**What to build:** A user can cancel a queued or running run. A queued run never starts; a running run stops at the next step boundary, and one blocked inside a long LLM or tool call is forcibly stopped by revoking its worker task. Every client sees the same outcome: status `cancelled`, a cancellation step, and a `done` event. Deleting a session cancels its active runs first, and a worker whose run disappears mid-flight stops quietly.

**Blocked by:** 05 (Tracer run)

**Status:** done

- [x] `DELETE /api/v1/runs/{id}` → atomic transition to cancelled from queued or running; sets the Redis cancel flag; revokes the Celery task (task id = run id) with terminate and SIGUSR1; returns the new status; already terminal → 409; another user's run → 404
- [x] Runner checks the cancel flag before each LLM call and each tool dispatch → records `cancelled` step, publishes `done` with status cancelled, stops
- [x] Task handles `SoftTimeLimitExceeded`: performs cleanup in a fresh short event loop — ensure status cancelled, record `cancelled` step, publish `done`
- [x] Conditional terminal writes guarantee a cancelled run is never overwritten to completed or failed
- [x] Queued run that is cancelled is a no-op if a worker later receives it (claim fails)
- [x] Session delete cancels all queued/running runs of that session through the same path before deleting
- [x] Runner treats a missing run row mid-flight as cancelled and stops without error
- [x] Tests: HTTP seam — cancel queued run (never executes), cancel terminal run 409, cross-user 404, session delete with an active run; runner seam — cancel flag mid-loop stops with cancelled step and no overwrite; run row deleted mid-loop stops quietly; Celery wrapper — `SoftTimeLimitExceeded` results in cancelled status, cancelled step and done event
