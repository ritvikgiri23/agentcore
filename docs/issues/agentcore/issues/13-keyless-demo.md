# 13: Keyless demo mode and demo page

**What to build:** Anyone can see the whole system working with no OpenAI key. With no key configured, the app falls back to a deterministic fake LLM and embedder that play the flagship scenario end to end — "What is 15% of the current population of Dubai, and remember that fact for me?" → web_search → calculator → remember_fact → final answer. A small demo page served by the API registers, creates a "Research Assistant" session, submits the prompt and renders the streamed steps live in the browser.

**Blocked by:** 07 (Real-time SSE stream), 08 (Long-term memory)

**Status:** ready-for-agent

- [ ] LLM provider setting defaults to `fake` when no OpenAI key is set, `openai` otherwise; the same choice drives the embedder
- [ ] Fake provider recognises the flagship prompt and plays search → calculator → remember_fact → final answer using real tool outputs; other prompts get a sensible scripted response
- [ ] `/demo` serves a static page (not under `/api/v1`) that registers or logs in, creates the session with web_search, calculator and remember_fact, submits the prompt and renders each SSE event via EventSource with the `access_token` query parameter, ending on `done`
- [ ] Makefile `demo` target runs the flagship scenario via curl against the running stack
- [ ] Tests (HTTP seam): with the fake provider selected by settings, the flagship prompt completes with the expected step sequence, a memory is stored, and the final answer contains the computed value; `/demo` returns the page
