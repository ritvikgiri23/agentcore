import json
from datetime import UTC, datetime, timedelta

import pytest

from app.llm import LLMUsage
from app.llm.fake import FakeReply, ScriptedLLM
from app.tools import ToolContext, ToolOutcome, dispatch, list_tool_names

BUILTIN_TOOLS = ["calculator", "get_current_datetime", "summarise_text", "web_search"]


async def calculate(expression: str, ctx: ToolContext) -> ToolOutcome:
    return await dispatch(
        "calculator", json.dumps({"expression": expression}), BUILTIN_TOOLS, ctx
    )


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("0.15 * 3655000", "548250"),
        ("2 + 3 * 4", "14"),
        ("(2 + 3) * 4", "20"),
        ("7 / 2", "3.5"),
        ("7 // 2", "3"),
        ("7 % 3", "1"),
        ("2 ** 10", "1024"),
        ("-5 + 2", "-3"),
        ("--4", "4"),
        ("0.1 + 0.2", "0.3"),
        ("2 ** -1", "0.5"),
    ],
)
async def test_calculator_evaluates_arithmetic(
    expression: str, expected: str, tool_context: ToolContext
) -> None:
    outcome = await calculate(expression, tool_context)

    assert (outcome.result, outcome.is_error) == (expected, False)


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo hi')",
        "x + 1",
        "abs(-3)",
        "(1).real",
        "[1, 2]",
        "1 if 1 else 2",
        "1 < 2",
        "True + 1",
        "1j * 2",
        "'a' * 3",
        "+5",
        "lambda: 1",
    ],
)
async def test_calculator_rejects_anything_but_arithmetic(
    expression: str, tool_context: ToolContext
) -> None:
    outcome = await calculate(expression, tool_context)

    assert outcome.is_error is True
    assert outcome.result.startswith("Error in 'calculator':")


@pytest.mark.parametrize(
    "expression",
    [
        "10 ** 100000",
        "(9 ** 999) ** 999",
        "2 ** 0.5 ** -5000",
        "10.0 ** 999",
        "9 ** 999 * 9 ** 999",
        "10 ** 999 / 1",
    ],
)
async def test_calculator_rejects_huge_results(
    expression: str, tool_context: ToolContext
) -> None:
    outcome = await calculate(expression, tool_context)

    assert outcome.is_error is True
    assert outcome.result.startswith("Error in 'calculator': CalculationError:")
    assert "too large" in outcome.result


@pytest.mark.parametrize("expression", ["1e999", "1e300 * 1e300", "1e999 - 1e999"])
async def test_calculator_rejects_non_finite_results(
    expression: str, tool_context: ToolContext
) -> None:
    outcome = await calculate(expression, tool_context)

    assert outcome.is_error is True
    assert "too large" in outcome.result


async def test_calculator_rejects_over_long_input(tool_context: ToolContext) -> None:
    outcome = await calculate("1 + " * 100 + "1", tool_context)

    assert outcome.is_error is True
    assert outcome.result == (
        "Invalid arguments for 'calculator': "
        "expression: String should have at most 200 characters"
    )


async def test_calculator_rejects_syntax_errors(tool_context: ToolContext) -> None:
    outcome = await calculate("2 +", tool_context)

    assert outcome.is_error is True
    assert outcome.result.startswith("Error in 'calculator':")


@pytest.mark.parametrize("expression", ["1 / 0", "1 // 0", "1 % 0"])
async def test_calculator_division_by_zero_is_an_error(
    expression: str, tool_context: ToolContext
) -> None:
    outcome = await calculate(expression, tool_context)

    assert outcome.is_error is True
    assert "division by zero" in outcome.result.lower()


def test_builtin_tools_are_registered() -> None:
    assert set(BUILTIN_TOOLS) <= set(list_tool_names())


async def search(query: str, ctx: ToolContext) -> list[dict[str, str]]:
    outcome = await dispatch("web_search", json.dumps({"query": query}), BUILTIN_TOOLS, ctx)
    assert outcome.is_error is False
    results: list[dict[str, str]] = json.loads(outcome.result)
    return results


async def test_web_search_returns_dubai_population_with_a_concrete_number(
    tool_context: ToolContext,
) -> None:
    results = await search("current population of Dubai", tool_context)

    assert len(results) == 3
    assert all(set(r) == {"title", "url", "snippet"} for r in results)
    assert "3,655,000" in results[0]["snippet"]


@pytest.mark.parametrize(
    ("query", "topic"),
    [("weather in London today", "weather"), ("Python release history", "python")],
)
async def test_web_search_has_fixtures_for_known_topics(
    query: str, topic: str, tool_context: ToolContext
) -> None:
    results = await search(query, tool_context)

    assert len(results) == 3
    assert all(topic in r["title"].lower() for r in results)
    assert all(query not in r["snippet"] for r in results)


async def test_web_search_falls_back_to_echoing_the_query(tool_context: ToolContext) -> None:
    results = await search("migratory patterns of lesser-known moths", tool_context)

    assert len(results) == 3
    assert all("migratory patterns of lesser-known moths" in r["snippet"] for r in results)


async def test_web_search_is_deterministic(tool_context: ToolContext) -> None:
    assert await search("dubai population", tool_context) == await search(
        "Dubai POPULATION", tool_context
    )


async def test_get_current_datetime_returns_utc_iso_timestamp(tool_context: ToolContext) -> None:
    outcome = await dispatch("get_current_datetime", "{}", BUILTIN_TOOLS, tool_context)

    stamp = datetime.fromisoformat(outcome.result)
    assert outcome.is_error is False
    assert stamp.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - stamp) < timedelta(seconds=5)


async def summarise(ctx: ToolContext, text: str, **args: object) -> ToolOutcome:
    return await dispatch(
        "summarise_text", json.dumps({"text": text, **args}), ["summarise_text"], ctx
    )


async def test_summarise_text_asks_for_a_word_limit_without_tools() -> None:
    llm = ScriptedLLM([FakeReply(content="A short summary.")])
    ctx = ToolContext(user_id="test-user", run_id="test-run", llm=llm)

    outcome = await summarise(ctx, "Some long material.", max_words=20)

    assert (outcome.result, outcome.is_error) == ("A short summary.", False)
    [request] = llm.requests
    assert request.tools == []
    assert request.messages[0]["role"] == "system"
    assert "at most 20 words" in request.messages[0]["content"]
    assert request.messages[1] == {"role": "user", "content": "Some long material."}


async def test_summarise_text_hard_truncates_to_max_words() -> None:
    llm = ScriptedLLM([FakeReply(content="  one two\nthree   four five six  ")])
    ctx = ToolContext(user_id="test-user", run_id="test-run", llm=llm)

    outcome = await summarise(ctx, "text", max_words=4)

    assert outcome.result == "one two three four"


async def test_summarise_text_adds_its_usage_to_the_context() -> None:
    llm = ScriptedLLM([FakeReply(content="Summary.", usage=LLMUsage(30, 7))])
    ctx = ToolContext(user_id="test-user", run_id="test-run", llm=llm)
    ctx.usage.add(LLMUsage(100, 10))

    await summarise(ctx, "text")

    assert (ctx.usage.prompt_tokens, ctx.usage.completion_tokens) == (130, 17)


@pytest.mark.parametrize("max_words", [0, 1001])
async def test_summarise_text_bounds_max_words(max_words: int) -> None:
    llm = ScriptedLLM()
    ctx = ToolContext(user_id="test-user", run_id="test-run", llm=llm)

    outcome = await summarise(ctx, "text", max_words=max_words)

    assert outcome.is_error is True
    assert outcome.result.startswith("Invalid arguments for 'summarise_text': max_words:")
    assert llm.requests == []
