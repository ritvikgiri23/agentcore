# 09: Short-term memory

**What to build:** The agent remembers the recent conversation within a session. After each successfully completed run, the user's message and the final answer are saved as a turn; the next run in that session sees the last five turns as context, so follow-up questions work. Broken turns (failed, cancelled, max-iterations) are never saved. Deleting a session clears its conversation history.

**Blocked by:** 05 (Tracer run)

**Status:** done

- [x] On completion with a real final answer, push `{user, assistant, run_id, ts}` onto the session's Redis list and trim to the retained-turn limit (20)
- [x] Context building reads the most recent turns for the window (5 turns = 10 messages, configurable), reversed into chronological order, inserted between the system message and the new user message
- [x] Failed, cancelled and max-iterations runs are not pushed
- [x] `memory_retrieval` step (if present from ticket 08) or context building records the short-term turn count
- [x] Session delete removes the session's Redis history key
- [x] Tests: HTTP seam — second run's LLM request contains the first turn; failed and max-iteration runs not stored; trimming to 20 retained and 5 used; session delete clears history
