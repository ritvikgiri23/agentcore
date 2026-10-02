"""The browser demo: one static page, served outside the versioned API."""

import html
from functools import cache
from pathlib import Path

from fastapi import APIRouter, status
from fastapi.responses import HTMLResponse

from app.llm.demo import FLAGSHIP_PROMPT

router = APIRouter(tags=["demo"])

_PAGE = Path(__file__).with_name("index.html")


@cache
def _page() -> str:
    # The prompt is filled in from the demo model's own constant, so the two can't drift.
    return _PAGE.read_text().replace("{{FLAGSHIP_PROMPT}}", html.escape(FLAGSHIP_PROMPT))


@router.get(
    "/demo",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
    summary="A browser demo that runs the flagship scenario and streams its steps live",
)
async def demo_page() -> HTMLResponse:
    return HTMLResponse(_page())
