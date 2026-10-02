"""The keyless demo model: plays the flagship scenario deterministically, with no API key.

It keeps no state between calls. Each reply is worked out from the conversation so far,
so the scenario advances on the real tool outputs: the population it multiplies is the
one the search returned, and the answer quotes what the calculator computed.
"""

import itertools
import json
import re
from typing import Any

from app.llm.base import ChatMessage, ChatResult, LLMUsage, ToolDefinition
from app.llm.fake import FakeReply, answer, call_tool, to_chat_result

FLAGSHIP_PROMPT = (
    "What is 15% of the current population of Dubai, and remember that fact for me?"
)
GENERIC_ANSWER = (
    "I'm AgentCore's offline demo model, so I can only play the demo scenario. "
    f'Try asking: "{FLAGSHIP_PROMPT}" Set OPENAI_API_KEY to get real answers to anything else.'
)

_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
# A comma-grouped figure such as 3,655,000.
_POPULATION = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")


class DemoLLM:
    def __init__(self) -> None:
        self._call_ids = itertools.count(1)

    async def chat(
        self, messages: list[ChatMessage], tools: list[ToolDefinition]
    ) -> ChatResult:
        offered = {t["function"]["name"] for t in tools}
        reply = _next_reply(messages, offered)
        # A rough count, about four characters a token, so traces show plausible usage.
        sent = len(json.dumps(messages)) + len(json.dumps(tools))
        produced = len(reply.content or "") + len(json.dumps(reply.tool_calls))
        usage = LLMUsage(prompt_tokens=sent // 4 + 1, completion_tokens=produced // 4 + 1)
        return to_chat_result(
            FakeReply(content=reply.content, tool_calls=reply.tool_calls, usage=usage),
            self._call_ids,
        )

    async def aclose(self) -> None:
        return None


def _next_reply(messages: list[ChatMessage], offered: set[str]) -> FakeReply:
    turn_start = max(i for i, m in enumerate(messages) if m["role"] == "user")
    question: str = messages[turn_start]["content"]
    lowered = question.lower()
    percent = _PERCENT.search(question)
    if percent is None or "dubai" not in lowered or "population" not in lowered:
        return answer(GENERIC_ANSWER)
    return _flagship(
        percent.group(1),
        wants_memory="remember" in lowered,
        outputs=_tool_outputs(messages[turn_start + 1 :]),
        offered=offered,
    )


def _flagship(
    percent: str, *, wants_memory: bool, outputs: dict[str, str], offered: set[str]
) -> FakeReply:
    """search → calculator → remember_fact → answer, one step per call."""
    if "web_search" not in outputs:
        return _call_if_offered("web_search", {"query": "current population of Dubai"}, offered)
    found = _population(outputs["web_search"])
    if found is None:
        return answer(
            "I searched for Dubai's population but couldn't find a figure in the results."
        )
    population, source = found
    if "calculator" not in outputs:
        expression = f"{population} * {percent} / 100"
        return _call_if_offered("calculator", {"expression": expression}, offered)
    value = _format_number(outputs["calculator"])
    if value is None:
        return answer(f"The calculator couldn't work it out: {outputs['calculator']}")
    summary = (
        f"Dubai's current population is about {population:,} ({source}), "
        f"so {percent}% of it is {value}."
    )
    if not wants_memory:
        return answer(summary)
    if "remember_fact" not in outputs:
        if "remember_fact" not in offered:
            return answer(f"{summary} {_not_enabled('remember_fact')}")
        fact = f"{percent}% of Dubai's population ({population:,}) is {value}."
        return call_tool("remember_fact", {"fact": fact})
    return answer(f"{summary} {_memory_note(outputs['remember_fact'])}")


def _call_if_offered(name: str, arguments: dict[str, Any], offered: set[str]) -> FakeReply:
    return call_tool(name, arguments) if name in offered else answer(_not_enabled(name))


def _not_enabled(name: str) -> str:
    return f"I'd need the {name} tool for this, but it isn't enabled for this session."


def _tool_outputs(turn: list[ChatMessage]) -> dict[str, str]:
    """This turn's tool results, by tool name."""
    names = {
        call["id"]: call["function"]["name"]
        for message in turn
        if message["role"] == "assistant"
        for call in message.get("tool_calls", [])
    }
    return {
        names[m["tool_call_id"]]: m["content"]
        for m in turn
        if m["role"] == "tool" and m["tool_call_id"] in names
    }


def _population(search_output: str) -> tuple[int, str] | None:
    """The first population figure in the search results, and the title of its source."""
    try:
        results = json.loads(search_output)
    except json.JSONDecodeError:
        # An error result, not search results.
        return None
    for result in results if isinstance(results, list) else []:
        match = _POPULATION.search(str(result.get("snippet", "")))
        if match:
            return int(match.group().replace(",", "")), str(result.get("title", "web search"))
    return None


def _format_number(calculator_output: str) -> str | None:
    try:
        value = float(calculator_output)
    except ValueError:
        return None
    return f"{int(value):,}" if value.is_integer() else f"{value:,.12g}"


def _memory_note(remember_output: str) -> str:
    if remember_output.startswith("Already remembered:"):
        return "I already had that in your long-term memory."
    if remember_output.startswith("Remembered:"):
        return "I've saved that to your long-term memory."
    return f"I couldn't save it to memory: {remember_output}"
