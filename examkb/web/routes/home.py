"""`/` -- what is in the corpus, and whether the projection still matches it.

The skeleton's only page with content. It deliberately shows the numbers 006
verified rather than a welcome message: if the counts on this page are wrong,
everything 010 onwards builds is wrong, and this is the cheapest place to see it.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from examkb.web.pages import render

router = APIRouter()


@router.get("/", response_class=HTMLResponse, name="home")
def home(request: Request) -> HTMLResponse:
    return render(request, "home.html")
