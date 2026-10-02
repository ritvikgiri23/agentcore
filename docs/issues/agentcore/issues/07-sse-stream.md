# 07: Real-time SSE stream

**What to build:** A user opens a Server-Sent Events stream for a run and watches every step arrive the moment it happens. A client that connects late — even after the run finished — first receives every persisted step in order, never sees duplicates, and always ends with a `done` event carrying the terminal status before the stream closes. Browser clients can authenticate with a query parameter because EventSource cannot send headers.

**Blocked by:** 05 (Tracer run)

**Status:** ready-for-agent

- [ ] `GET /api/v1/runs/{id}/stream` returns `text/event-stream`; ownership checked before subscribing (another user's run → 404)
- [ ] Algorithm: subscribe to the run channel (buffering) → replay persisted steps in order, remembering their ids → if terminal, emit `done` and close → otherwise relay live events skipping already-sent ids until terminal, then emit `done` and close
- [ ] Frames are `data: {"step_type": …, "payload": …}\n\n`; final frame `data: {"step_type": "done", "status": "<terminal status>"}\n\n`
- [ ] Keepalive comment roughly every 15 seconds
- [ ] Client disconnect detected → unsubscribe and return without error logging
- [ ] `get_current_user_sse` accepts Bearer header or `access_token` query parameter; used only by this route; the query token is scrubbed from logs
- [ ] Tests (HTTP seam): finished run → full replay then done; in-progress run → replayed plus live steps with no duplicates then done; failed run ends with done/failed; query-parameter token accepted; missing token 401; another user's run 404
