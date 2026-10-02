# 04: Agent sessions

**What to build:** An authenticated user can create agent sessions (name, system prompt, enabled tools), list them with pagination, view one, and delete one. Requesting an unknown tool fails with a clear 422 that names the unknown tools and lists the available ones. Users can never see or touch another user's sessions — those return 404 as if they did not exist.

**Blocked by:** 02 (Auth), 03 (Tool registry and dispatcher)

**Status:** ready-for-agent

- [ ] Agent sessions table as specified (uuid id, user id FK indexed, name, system prompt, tools enabled array, created at) with migration; relationships use `raise` loading
- [ ] Ownership repository with an owned-session lookup that filters by the caller's user id in the query; not found and not owned both → 404
- [ ] `POST /api/v1/sessions` → 201; validator checks tools against the registry → 422 `unknown_tool` with details `{unknown, available}`; duplicate tool names de-duplicated; name 1–100 chars, system prompt ≤ 4,000 chars
- [ ] `GET /api/v1/sessions` → `{items, total, limit, offset}` (limit default 50, max 200), caller's sessions only
- [ ] `GET /api/v1/sessions/{id}` → session detail (recent runs list present but empty until ticket 05)
- [ ] `DELETE /api/v1/sessions/{id}` → 204
- [ ] Session factory fixture
- [ ] Routes declare response model, status code, error responses and summary; logs bind user id and session id
- [ ] Tests (HTTP seam): create happy path, unknown tool 422 shape, de-duplication, size limits, pagination, detail, delete, and 404 for another user's session on get and delete; unauthenticated → 401
