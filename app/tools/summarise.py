from typing import Annotated

from pydantic import Field

from app.tools.registry import ToolContext, tool

MAX_TEXT_LENGTH = 50_000
MAX_SUMMARY_WORDS = 1000


@tool
async def summarise_text(
    text: Annotated[
        str, Field(description="The text to summarise", min_length=1, max_length=MAX_TEXT_LENGTH)
    ],
    ctx: ToolContext,
    max_words: Annotated[
        int, Field(description="Maximum length of the summary in words", ge=1, le=MAX_SUMMARY_WORDS)
    ] = 100,
) -> str:
    """Summarise text in at most `max_words` words, keeping the key facts and figures."""
    if ctx.llm is None:
        raise RuntimeError("No LLM provider is available to summarise with")
    result = await ctx.llm.chat(
        [
            {
                "role": "system",
                "content": f"Summarise the user's text in at most {max_words} words. "
                "Keep the key facts and figures. Reply with the summary only.",
            },
            {"role": "user", "content": text},
        ],
        [],
    )
    # Nested calls count toward the run's cost like any other.
    ctx.usage.add(result.usage)
    # Models overshoot word limits; the limit is a promise to the caller, so enforce it.
    return " ".join((result.content or "").split()[:max_words])
