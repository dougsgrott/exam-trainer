"""`/results/{attempt_id}` -- the end of the core loop.

Two things this route is careful about.

It **refuses an open attempt**. The page renders the answer key, so a results URL
that worked mid-exam would make 015's "the runner never shows the key" worth
nothing: you would open the other tab. `ResultsNotReady` becomes a 409 with the
reason, not a 404 that looks like a typo.

It **grades on arrival**. 014's `grade_attempt` is idempotent for exactly this --
the page is a URL somebody reloads, and the second visit reads what the first one
wrote rather than re-stamping it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from examkb.services import results as results_service
from examkb.web.pages import database_url, render

router = APIRouter()


@router.get("/results", response_class=HTMLResponse, name="results_index")
def results_index(request: Request) -> HTMLResponse:
    return render(
        request,
        "results_index.html",
        attempts=results_service.recent(url=database_url(request)),
    )


@router.get("/results/{attempt_id}", response_class=HTMLResponse, name="results")
def results(request: Request, attempt_id: int) -> HTMLResponse:
    try:
        page = results_service.page(attempt_id, url=database_url(request))
    except results_service.ResultsNotReady as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except results_service.GradingError as error:  # pragma: no cover -- submitted implies gradable
        raise HTTPException(status_code=409, detail=str(error)) from error
    if page is None:
        raise HTTPException(status_code=404, detail=f"No attempt {attempt_id}")
    return render(request, "results.html", page=page)
