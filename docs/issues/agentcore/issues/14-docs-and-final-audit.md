# 14: Docs and final audit

**What to build:** A reviewer can understand, run and evaluate AgentCore from the repository alone. The README gets them running in minutes, NOTES explains the thinking and every deliberate deviation from the brief, ADRs capture the five biggest decisions, coverage meets the bar, the OpenAPI docs are complete, and the code is published as a public GitHub repository.

**Blocked by:** 01–13

**Status:** ready-for-agent

- [ ] README: quickstart (`cp .env.example .env && docker compose up`), the `/api/v1` mapping to the brief's paths, curl walkthrough of the Dubai scenario (register → session → run → stream → status → steps → memory search), running tests, fake vs OpenAI mode, demo page
- [ ] NOTES.md: agent loop design, memory strategy (short-term window vs retention, long-term retrieval and dedup, eviction), every deviation from the brief with rationale (access token only, AST calculator, async tools, `raise` loading, extra step types, extra columns, `/api/v1` prefix, rate-limit set), prepared answers to the review questions (stop conditions, hallucinated tools, POST→SSE trace through Redis and Postgres, pgvector cost and index with the filtered-ANN caveat, concurrency and isolation), and future work (stream tickets, real search provider, Redis Streams, iterative HNSW scans/partitioning, refresh tokens)
- [ ] ADRs in `docs/adr/`: Celery/asyncio bridge, SSE replay-then-live, atomic claim + heartbeat lease, tool registry design, rate-limit set + Lua admission
- [ ] Test coverage ≥ 75% (report produced by `make test`)
- [ ] Every route audited for response model, status code, error responses and summary; Swagger UI reviewed
- [ ] Public GitHub repository created and pushed with the build-order commit history
