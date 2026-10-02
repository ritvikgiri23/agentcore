"""The agent loop: LLM call → tool dispatch → feed results back, until a plain answer."""

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.llm import ChatMessage, ChatResult, Embedder, LLMProvider, ToolCallRequest
from app.memory import long_term, short_term
from app.memory.long_term import RecalledMemory
from app.memory.short_term import Turn
from app.models import AgentRun, AgentSession, RunStatus, StepType
from app.runs import lifecycle
from app.runs.events import EventPublisher, publish_done
from app.tools import ToolContext, ToolOutcome, dispatch, get_tool_definitions

logger = structlog.get_logger(__name__)

MAX_ITERATIONS_MESSAGE = "Max iterations reached"

_MEMORY_BLOCK_HEADER = (
    "Relevant memories about the user, from earlier conversations. They may be outdated "
    "or no longer true; prefer what the user says now."
)


@dataclass(frozen=True)
class RunnerDeps:
    """Per-task resources; the caller creates and disposes them."""

    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    llm: LLMProvider
    embedder: Embedder


async def run_agent(run_id: str, deps: RunnerDeps) -> None:
    """Execute a queued run to completion. Any run not in `queued` is left untouched."""
    with structlog.contextvars.bound_contextvars(run_id=run_id):
        async with deps.session_factory() as db:
            run = await lifecycle.claim(db, run_id)
            if run is None:
                logger.info("run_not_claimed")
                return
            agent_session = await db.get(AgentSession, run.session_id)
            if agent_session is None:
                # Unreachable while the session FK cascades, but never strand a claimed run.
                logger.warning("run_session_missing")
                if await lifecycle.fail(db, run_id):
                    await publish_done(deps.redis, run_id, RunStatus.FAILED)
                return
        structlog.contextvars.bind_contextvars(session_id=agent_session.id)
        logger.info("run_started")
        await _AgentLoop(run, agent_session, deps).run()


class _AgentLoop:
    def __init__(self, run: AgentRun, agent_session: AgentSession, deps: RunnerDeps) -> None:
        self._settings = get_settings()
        self._run_id = run.id
        self._session_id = agent_session.id
        self._user_message = run.user_message
        self._user_id = agent_session.user_id
        self._system_prompt = agent_session.system_prompt
        self._deps = deps
        self._events = EventPublisher(deps.session_factory, deps.redis)
        self._enabled_tools = list(agent_session.tools_enabled)
        self._tool_definitions = get_tool_definitions(self._enabled_tools)
        self._ctx = ToolContext(
            user_id=agent_session.user_id,
            run_id=run.id,
            session_factory=deps.session_factory,
            llm=deps.llm,
            embedder=deps.embedder,
        )
        self._messages: list[ChatMessage] = []
        self._completed = False

    async def run(self) -> None:
        try:
            await self._loop()
        except Exception as exc:
            if self._completed:
                # Only the `done` publish can fail now; the run's outcome is already settled.
                logger.exception("run_done_not_published")
                return
            await self._fail(exc)

    async def _loop(self) -> None:
        memories, turns = await self._retrieve_memories()
        system = "\n\n".join(
            part for part in (self._system_prompt, _memory_block(memories)) if part
        )
        if system:
            self._messages.append({"role": "system", "content": system})
        for turn in turns:
            self._messages.extend(turn.as_messages())
        self._messages.append({"role": "user", "content": self._user_message})

        max_iterations = self._settings.max_iterations
        for iteration in range(1, max_iterations + 1):
            result = await self._call_llm(iteration)
            if not result.tool_calls:
                await self._finish(result.content or "", iterations=iteration)
                return
            self._messages.append(result.as_assistant_message())
            await self._call_tools(iteration, result.tool_calls)
        await self._finish(MAX_ITERATIONS_MESSAGE, iterations=max_iterations, cap_hit=True)

    async def _retrieve_memories(self) -> tuple[list[RecalledMemory], list[Turn]]:
        turns = await short_term.recent_turns(
            self._deps.redis,
            self._session_id,
            # A turn is two messages: the user's and the answer.
            limit=self._settings.short_term_context_messages // 2,
        )
        memories: list[RecalledMemory] = []
        top_k = self._settings.long_term_top_k
        with structlog.contextvars.bound_contextvars(
            step_type=StepType.MEMORY_RETRIEVAL.value
        ):
            embedding: list[float] | None = None
            if top_k > 0:
                try:
                    embedding = await self._deps.embedder.embed(self._user_message)
                except Exception:
                    # Memories only add context; an embedding outage must not block answering.
                    logger.exception("memory_embedding_failed")
            if embedding is not None:
                async with self._deps.session_factory() as db:
                    memories = await long_term.search(
                        db,
                        self._user_id,
                        embedding,
                        limit=top_k,
                        max_distance=self._settings.memory_max_distance,
                    )
        await self._events.record(
            self._run_id,
            StepType.MEMORY_RETRIEVAL,
            {
                "memories": [
                    {
                        "id": m.memory.id,
                        "content": m.memory.content,
                        "distance": round(m.distance, 6),
                    }
                    for m in memories
                ],
                "short_term_turns": len(turns),
            },
        )
        return memories, turns

    async def _call_llm(self, iteration: int) -> ChatResult:
        with structlog.contextvars.bound_contextvars(step_type=StepType.LLM_CALL.value):
            result = await self._deps.llm.chat(self._messages, self._tool_definitions)
        self._ctx.usage.add(result.usage)
        async with self._deps.session_factory() as db:
            await lifecycle.set_tokens_used(db, self._run_id, self._ctx.usage.total_tokens)
        await self._events.record(
            self._run_id,
            StepType.LLM_CALL,
            {
                "iteration": iteration,
                "model": result.model,
                "finish_reason": result.finish_reason,
                "content": result.content,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments}
                    for c in result.tool_calls
                ],
                "usage": {
                    "prompt_tokens": result.usage.prompt_tokens,
                    "completion_tokens": result.usage.completion_tokens,
                    "total_tokens": result.usage.total_tokens,
                },
                "latency_ms": round(result.latency_ms, 2),
            },
        )
        return result

    async def _call_tools(self, iteration: int, calls: list[ToolCallRequest]) -> None:
        # Steps are recorded one at a time, in call order; only the tools themselves overlap.
        for call in calls:
            await self._events.record(
                self._run_id,
                StepType.TOOL_CALL,
                {
                    "iteration": iteration,
                    "tool_call_id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                },
            )
        with structlog.contextvars.bound_contextvars(step_type=StepType.TOOL_CALL.value):
            outcomes = await asyncio.gather(
                *(
                    dispatch(
                        call.name,
                        call.arguments,
                        self._enabled_tools,
                        self._ctx,
                        timeout=self._settings.tool_timeout_seconds,
                    )
                    for call in calls
                )
            )
        for call, outcome in zip(calls, outcomes, strict=True):
            await self._record_outcome(call, outcome)
            # Every tool call id needs a reply, error or not; the model sees the full result.
            self._messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": outcome.result}
            )

    async def _record_outcome(self, call: ToolCallRequest, outcome: ToolOutcome) -> None:
        if outcome.timed_out:
            await self._events.record(
                self._run_id,
                StepType.TOOL_TIMEOUT,
                {
                    "tool_call_id": call.id,
                    "name": call.name,
                    "timeout_seconds": self._settings.tool_timeout_seconds,
                },
            )
            return
        limit = self._settings.tool_result_max_chars
        payload: dict[str, Any] = {
            "tool_call_id": call.id,
            "name": call.name,
            "result": outcome.result[:limit],
            "truncated": len(outcome.result) > limit,
            "is_error": outcome.is_error,
            "latency_ms": round(outcome.latency_ms, 2),
        }
        await self._events.record(self._run_id, StepType.TOOL_RESULT, payload)

    async def _finish(self, content: str, *, iterations: int, cap_hit: bool = False) -> None:
        await self._events.record(
            self._run_id,
            StepType.FINAL_ANSWER,
            {"content": content, "iterations": iterations, "max_iterations_hit": cap_hit},
        )
        async with self._deps.session_factory() as db:
            completed = await lifecycle.complete(
                db,
                self._run_id,
                final_answer=content,
                tokens_used=self._ctx.usage.total_tokens,
            )
        self._completed = completed
        if not completed:
            # Someone else (e.g. a cancellation) already finished the run and owns `done`.
            logger.info("run_completion_skipped")
            return
        logger.info("run_completed", iterations=iterations, max_iterations_hit=cap_hit)
        # Only a real answer is worth recalling; a capped or empty one would mislead.
        if content and not cap_hit:
            await self._remember_turn(content)
        await self._events.publish_done(self._run_id, RunStatus.COMPLETED)

    async def _remember_turn(self, answer: str) -> None:
        # Pushed before `done`, so a follow-up sent on `done` already sees this turn.
        try:
            await short_term.push_turn(
                self._deps.redis,
                self._session_id,
                Turn(
                    user=self._user_message,
                    assistant=answer,
                    run_id=self._run_id,
                    ts=time.time(),
                ),
                retained=self._settings.short_term_retained_turns,
            )
        except Exception:
            # The run is already completed; losing the turn only costs follow-up context.
            logger.exception("short_term_turn_not_saved")

    async def _fail(self, exc: Exception) -> None:
        # The traceback stays in the logs; the trace only says what went wrong.
        logger.exception("run_failed")
        try:
            await self._events.record(
                self._run_id, StepType.ERROR, {"type": type(exc).__name__, "message": str(exc)}
            )
        except Exception:
            # Whatever broke may also break recording; the run must still end as failed.
            logger.exception("run_error_step_not_recorded")
        async with self._deps.session_factory() as db:
            failed = await lifecycle.fail(
                db, self._run_id, tokens_used=self._ctx.usage.total_tokens
            )
        if not failed:
            logger.info("run_failure_skipped")
            return
        await self._events.publish_done(self._run_id, RunStatus.FAILED)


def _memory_block(memories: list[RecalledMemory]) -> str:
    """The delimited memories section of the system message; empty when there are none."""
    if not memories:
        return ""
    lines = "\n".join(f"- {m.memory.content}" for m in memories)
    return f"<relevant_memories>\n{_MEMORY_BLOCK_HEADER}\n{lines}\n</relevant_memories>"
