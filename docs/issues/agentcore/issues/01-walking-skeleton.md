# 01: Walking skeleton

**What to build:** A fresh clone becomes a running, testable system with one command. `docker compose up` starts PostgreSQL (with pgvector), Redis, a one-shot migration step, the API and a Celery worker in the correct order, and the API answers a health check under `/api/v1`. Every error the API returns already uses the consistent error envelope with a request id, logs are structured JSON, and the test harness is in place for every later ticket. The repository is initialised with git.

**Blocked by:** None (can start immediately)

**Status:** done

- [x] Git repository initialised with a sensible `.gitignore` (env files, caches, virtualenvs); `.env.example` documents every setting
- [x] Python 3.12 project managed with `uv` and a lockfile; one app image used by the API, worker and migration services
- [x] Settings load from environment: OpenAI key (optional), JWT secret and expiry, database URL, Redis URL, LLM provider, model names and all tunables listed in the spec
- [x] Compose stack: `pgvector/pgvector:pg16` and `redis:7-alpine` with healthchecks; a `migrate` service runs Alembic upgrade and exits; API and worker start only after migrate completes successfully; worker uses the prefork pool
- [x] Postgres init creates the test database alongside the main one; the initial migration enables the `vector` extension
- [x] Dev override file mounts source and enables reload; base compose file stays production-like
- [x] Makefile targets for up, test, migrate and demo (demo may be a placeholder)
- [x] `GET /api/v1/health` returns 200
- [x] Error envelope `{error: {code, message, details, request_id}}` rendered for application errors, request validation errors, HTTP exceptions and unhandled exceptions (generic message, no internals leaked)
- [x] Request-id middleware reads or creates `X-Request-ID`, echoes it on every response and binds it into the log context
- [x] structlog JSON logging with context for request id (user/session/run/step fields bindable later); access tokens in query strings scrubbed from request logs
- [x] Test harness: async HTTP client fixture, test database with tables created once per session and per-test transaction rollback, async fakeredis fixture
- [x] Tests cover the health endpoint, the error envelope for a 404 and a forced 500, and request-id echoing
