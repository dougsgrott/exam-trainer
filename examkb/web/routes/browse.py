"""`/browse` and `/questions/{id}` -- the replacement for the 3 MB `browse.html`.

The static report inlines all 549 questions as JSON so the browser can filter
them. This does the filtering where the corpus already is, and ships one page of
results; the whole point is that the page size stops depending on the corpus size.

Both routes are thin on purpose. `examkb/services/browse.py` opens the session and
returns frozen dataclasses; what happens here is reading query parameters off the
request and choosing a template. That is the layering rule (008) doing its job
rather than being recited.

The detail route is `{question_id:path}` because a question id contains slashes --
`ccao-f/exam-01/q001` is one identifier, not three path segments.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from examkb.services import browse as browse_service
from examkb.web.pages import database_url, render

router = APIRouter()


@router.get("/browse", response_class=HTMLResponse, name="browse")
def browse(
    request: Request,
    q: str = Query("", description="Full-text search over prompt, options and explanations."),
    cert: str | None = None,
    exam: str | None = None,
    mode: str | None = None,
    domain: str | None = None,
    type: str | None = None,  # noqa: A002 -- it is the URL parameter's name
    mark: str | None = None,
    page: int = 1,
    per_page: int = browse_service.DEFAULT_PER_PAGE,
) -> HTMLResponse:
    """Every filter is a query parameter, so a view is a link somebody can send.

    The parameter names are `browse_service.FILTER_PARAMS`' keys, and declaring
    them here is the one place that has to agree by hand. `test_marks.py` drives
    every one of them over HTTP for exactly that reason -- 011 added `mark` to the
    map, the facet and the template, and this signature was the thing that got
    missed.
    """
    result = browse_service.browse_page(
        q=q,
        params={
            "cert": cert,
            "exam": exam,
            "mode": mode,
            "domain": domain,
            "type": type,
            "mark": mark,
        },
        page=page,
        per_page=per_page,
        url=database_url(request),
    )
    return render(request, "browse.html", browse=result)


@router.get("/questions/{question_id:path}", response_class=HTMLResponse, name="question")
def question(request: Request, question_id: str) -> HTMLResponse:
    detail = browse_service.question_page(question_id, url=database_url(request))
    if detail is None:
        raise HTTPException(status_code=404, detail=f"No question {question_id!r}")
    return render(request, "question_detail.html", question=detail)
