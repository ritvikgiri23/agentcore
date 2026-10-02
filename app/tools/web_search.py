import json
from typing import Annotated, TypedDict

from pydantic import Field

from app.tools.registry import tool


class SearchResult(TypedDict):
    title: str
    url: str
    snippet: str


# Deterministic stand-in for a real search provider: the first entry whose keywords all
# appear in the query wins.
_CANNED_RESULTS: list[tuple[tuple[str, ...], list[SearchResult]]] = [
    (
        ("dubai", "population"),
        [
            {
                "title": "Dubai Population 2025 - Dubai Statistics Center",
                "url": "https://www.dsc.gov.ae/en-us/Themes/Pages/Population-and-Vital-Statistics.aspx",
                "snippet": "The total population of Dubai is estimated at 3,655,000 residents "
                "as of 2025, according to the Dubai Statistics Center.",
            },
            {
                "title": "Dubai population passes 3.6 million - Gulf News",
                "url": "https://gulfnews.com/uae/dubai-population-passes-36-million",
                "snippet": "Dubai's population has grown steadily, passing 3.6 million as "
                "expatriate arrivals continue to rise.",
            },
            {
                "title": "Demographics of Dubai - Wikipedia",
                "url": "https://en.wikipedia.org/wiki/Demographics_of_Dubai",
                "snippet": "Around 90% of Dubai's residents are expatriates, with the largest "
                "communities from India, Pakistan and Bangladesh.",
            },
        ],
    ),
    (
        ("weather",),
        [
            {
                "title": "Weather forecast - Met Office",
                "url": "https://www.metoffice.gov.uk/weather/forecast",
                "snippet": "Partly cloudy with a high of 21°C and a low of 13°C. "
                "Light winds from the south-west.",
            },
            {
                "title": "Hourly weather - BBC Weather",
                "url": "https://www.bbc.co.uk/weather",
                "snippet": "Sunny spells this afternoon, with a 10% chance of rain "
                "turning to showers overnight.",
            },
            {
                "title": "10-day weather outlook - AccuWeather",
                "url": "https://www.accuweather.com/",
                "snippet": "Temperatures stay mild through the week, with rain likely "
                "on Thursday.",
            },
        ],
    ),
    (
        ("python",),
        [
            {
                "title": "Welcome to Python.org",
                "url": "https://www.python.org/",
                "snippet": "Python is a programming language that lets you work quickly "
                "and integrate systems more effectively.",
            },
            {
                "title": "Python release schedule - Python Developer's Guide",
                "url": "https://devguide.python.org/versions/",
                "snippet": "Python 3.13 was released in October 2024; each feature release "
                "is supported for five years.",
            },
            {
                "title": "Python (programming language) - Wikipedia",
                "url": "https://en.wikipedia.org/wiki/Python_(programming_language)",
                "snippet": "Python was created by Guido van Rossum and first released "
                "in 1991.",
            },
        ],
    ),
]


@tool
def web_search(
    query: Annotated[
        str, Field(description="What to search the web for", min_length=1, max_length=500)
    ],
) -> str:
    """Search the web and return the top three results as JSON (title, url, snippet)."""
    normalised = query.lower()
    for keywords, results in _CANNED_RESULTS:
        if all(keyword in normalised for keyword in keywords):
            return json.dumps(results)
    return json.dumps(_fallback(query))


def _fallback(query: str) -> list[SearchResult]:
    return [
        {
            "title": f"{query} - Overview",
            "url": "https://example.com/search?result=1",
            "snippet": f"An overview of {query}, covering the key facts and background.",
        },
        {
            "title": f"{query} - Latest news",
            "url": "https://example.com/search?result=2",
            "snippet": f"Recent reporting and developments related to {query}.",
        },
        {
            "title": f"{query} - Frequently asked questions",
            "url": "https://example.com/search?result=3",
            "snippet": f"Common questions and answers about {query}.",
        },
    ]
