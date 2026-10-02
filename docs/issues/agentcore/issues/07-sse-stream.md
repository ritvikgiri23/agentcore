# 07: Real-time SSE stream

**What to build:** A user opens a Server-Sent Events stream for a run and watches every step arrive the moment it happens. A client that connects late — even after the run finished — first receives every persisted step in order, never sees duplicates, and always ends with a `done` event carrying the terminal status before the stream closes. Browser clients can authenticate with a query parameter because EventSource cannot send headers.

**Blocked by:** 05 (Tracer run)

**Status:** done

- [x] `GET /api/v1/runs/{id}/stream` returns `text/event-stream`; ownership checked before subscribing (another user's run → 404)
- [x] Algorithm: subscribe to the run channel (buffering) → replay persisted steps in order, remembering their ids → if terminal, emit `done` and close → otherwise relay live events skipping already-sent ids until terminal, then emit `done` and close
- [x] Frames are `data: {"step_type": …, "payload": …}\n\n`; final frame `data: {"step_type": "done", "status": "<terminal status>"}\n\n`
- [x] Keepalive comment roughly every 15 seconds
- [x] Client disconnect detected → unsubscribe and return without error logging
- [x] `get_current_user_sse` accepts Bearer header or `access_token` query parameter; used only by this route; the query token is scrubbed from logs
- [x] Tests (HTTP seam): finished run → full replay then done; in-progress run → replayed plus live steps with no duplicates then done; failed run ends with done/failed; query-parameter token accepted; missing token 401; another user's run 404

## Comments

- From ticket 07: notes for later tickets.
  - **Beyond the ticket:** frames carry the published event as-is (`id`, `step_type`, `payload`, `occurred_at`), which is more than the spec's `{step_type, payload}`. A stream with no live events for `STATUS_RECHECK_SECONDS` (15s, `app/runs/stream.py`) rereads the run's status from Postgres, so a lost `done` still ends it. Before `done`, persisted steps that were never relayed are sent, so a lost publish is recovered, though possibly out of order. A run deleted mid-stream ends with `done`/`cancelled`. Keepalive (`: ping` every 15s) and disconnect cancellation come from FastAPI's built-in `EventSourceResponse`.
  - **11:** the stream reads a run's status before its steps and relies on "a terminal run has all of its steps persisted". Cancellation must write the `cancelled` step before, or in the same transaction as, the status change. Otherwise a client connecting in between gets `done/cancelled` without the step.
  - **13:** frames have no SSE `id:` field, and the server closes the stream after `done`. Native `EventSource` reconnects when a stream closes, so the demo page must call `close()` when it receives `done`, or it will replay the whole run again.
  - **14 (NOTES/ADR):** a Redis error mid-stream ends the stream with an error. There is no fallback to polling Postgres. Deployment note: the dev compose reloader did not pick up file changes from the macOS bind mount; `docker compose restart api worker` did.
