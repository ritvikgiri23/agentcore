from typing import Annotated

from pydantic import AfterValidator, StringConstraints

from app.core.config import get_settings
from app.memory import long_term
from app.schemas.sessions import reject_nul
from app.tools.registry import ToolContext, tool

MAX_FACT_LENGTH = 1000


@tool
async def remember_fact(
    fact: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_FACT_LENGTH),
        AfterValidator(reject_nul),
    ],
    ctx: ToolContext,
) -> str:
    """Store a durable fact about the user in long-term memory, so it can be recalled in
    later conversations. Use it for stable facts and preferences, phrased as a standalone
    sentence; not for transient details of the current task."""
    if ctx.session_factory is None or ctx.embedder is None:
        raise RuntimeError("Long-term memory is not available")
    embedding = await ctx.embedder.embed(fact)
    async with ctx.session_factory() as db:
        nearest = await long_term.search(db, ctx.user_id, embedding, limit=1)
        if nearest and nearest[0].distance <= get_settings().memory_dedup_distance:
            return f"Already remembered: {nearest[0].memory.content}"
        await long_term.insert(db, ctx.user_id, fact, embedding, source_run_id=ctx.run_id)
    return f"Remembered: {fact}"
