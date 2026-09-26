"""`/exam` -- sitting one.

The whole reason this file is careful is integration fix 3. Answering a question
must not replace the DOM the person is clicking in: on a 2-of-4, the second click
would land on a node HTMX had already detached, and the answer would submit with
one selection. The person sees a question they answered correctly marked wrong,
and nothing appears in any log.

So **no answer response has a body**. `POST .../answer` returns `204 No Content`
with an `HX-Trigger` header, the form declares `hx-swap="none"`, and the browser's
own checkboxes stay exactly where they were. `runner.js` listens for the trigger
and updates the counter, the map and the save indicator -- none of which contain
an input.

Two more things this file does not do, both on purpose: it never renders the
answer key (the snapshot holds one; `RunnerItem` does not carry it), and it never
renders a chat entry point, because the 041 gate refuses a chat thread while an
attempt is open and a button that always errors is worse than no button.

Submitting redirects to `/results/{id}` (016). There is no placeholder page any
more -- 015 shipped one and said 016 would take its job, and it has.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from examkb.services import runner as runner_service
from examkb.web.pages import database_url, render

router = APIRouter()

TRIGGER = "examkb:saved"


def _saved(result: runner_service.SaveResult) -> Response:
    """204, and the whole answer is in a header.

    An empty body is not an optimisation. It is the guarantee: there is nothing
    for HTMX to swap in, so there is no way for a response to remove the control
    that is being clicked.
    """
    return Response(
        status_code=204,
        headers={"HX-Trigger": json.dumps({TRIGGER: result.payload()})},
    )


@router.get("/exam", response_class=HTMLResponse, name="exams")
def exam_start(request: Request) -> HTMLResponse:
    """Pick a certification, or resume the sitting already in progress."""
    return render(request, "exam_start.html", start=runner_service.start_page(
        url=database_url(request)
    ))


@router.post("/exam/start", name="exam_begin")
def exam_begin(
    request: Request,
    certification_id: str = Form(...),
    count: int | None = Form(None),
    seed: int | None = Form(None),
) -> Response:
    try:
        attempt_id = runner_service.begin(
            certification_id=certification_id,
            count=count or None,
            seed=seed,
            url=database_url(request),
        )
    except runner_service.AttemptError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse(f"/exam/{attempt_id}", status_code=303)


@router.get("/exam/{attempt_id}", name="exam_resume")
def exam_resume(request: Request, attempt_id: int) -> Response:
    """Drop in at the first unanswered question, which is where you left off."""
    position = runner_service.first_unanswered(attempt_id, url=database_url(request))
    return RedirectResponse(f"/exam/{attempt_id}/q/{position}", status_code=303)


@router.get("/exam/{attempt_id}/q/{position}", response_class=HTMLResponse, name="exam_question")
def exam_question(request: Request, attempt_id: int, position: int) -> HTMLResponse:
    page = runner_service.page(attempt_id, position, url=database_url(request))
    if page is None:
        raise HTTPException(status_code=404, detail=f"No attempt {attempt_id}")
    if not page.open:
        return RedirectResponse(f"/results/{attempt_id}", status_code=303)
    return render(request, "runner.html", page=page)


@router.post("/exam/{attempt_id}/answer", name="exam_answer")
def exam_answer(
    request: Request,
    attempt_id: int,
    position: int = Form(...),
    label: list[str] = Form(default=[]),
    time_ms: int = Form(0),
) -> Response:
    """204 and nothing else. The position comes from the form, never from a cursor."""
    try:
        result = runner_service.save_answer(
            attempt_id, position, label, time_ms=time_ms, url=database_url(request)
        )
    except runner_service.AttemptError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _saved(result)


@router.post("/exam/{attempt_id}/flag", name="exam_flag")
def exam_flag(request: Request, attempt_id: int, position: int = Form(...)) -> Response:
    try:
        result = runner_service.toggle_flag(attempt_id, position, url=database_url(request))
    except runner_service.AttemptError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return _saved(result)


@router.post("/exam/{attempt_id}/submit", name="exam_submit")
def exam_submit(request: Request, attempt_id: int) -> Response:
    try:
        runner_service.finish(attempt_id, url=database_url(request))
    except runner_service.AttemptError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse(f"/results/{attempt_id}", status_code=303)


@router.post("/exam/{attempt_id}/abandon", name="exam_abandon")
def exam_abandon(request: Request, attempt_id: int) -> Response:
    try:
        runner_service.walk_away(attempt_id, url=database_url(request))
    except runner_service.AttemptError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse("/exam", status_code=303)
