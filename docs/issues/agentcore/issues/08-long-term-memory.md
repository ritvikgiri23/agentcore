# 08: Long-term memory

**What to build:** The agent can remember durable facts about a user and recall the relevant ones in later runs, across sessions. When asked to remember something, it embeds and stores the fact (skipping near-duplicates). At the start of every run, up to three relevant memories are added to the agent's context and shown in the trace. Users can list, semantically search and delete their own memories — and only their own.

**Blocked by:** 05 (Tracer run)

**Status:** done

- [x] Long-term memory table as specified (1536-dim vector; source run FK `ON DELETE SET NULL`) with B-tree index on user id and HNSW index on embedding using `vector_cosine_ops`; migration
- [x] Embedder abstraction: OpenAI `text-embedding-3-small` and a deterministic hash-based fake (identical text → identical vector)
- [x] `remember_fact` (async): embeds, checks nearest existing memory for the user against the dedup distance → "Already remembered: …", otherwise inserts with source run id → "Remembered: …"
- [x] Runner retrieval: embed the user message, fetch top 3 for the user with an optional max-distance cutoff, append them to the system message in a delimited "relevant memories" block with a may-be-outdated caveat (omit the block if none), record a `memory_retrieval` step (ids, content, distances); embedding failure → logged, run continues without memories
- [x] `GET /api/v1/memory` → paginated memories (id, content, source run id, created at)
- [x] `GET /api/v1/memory/search?q=` → top 5 `{id, content, distance, source_run_id, created_at}`
- [x] `DELETE /api/v1/memory/{id}` → 204; another user's memory → 404
- [x] Tests: dispatcher seam — remember_fact inserts, dedups, records source run; runner seam — memories injected and step recorded, cutoff filters irrelevant ones, embedding failure tolerated; HTTP seam — list, search ordering with the fake embedder, delete, cross-user isolation on all three routes
