"""Toggling a mark, over HTMX, without redrawing the page it was clicked on.

One endpoint, and it returns a fragment rather than a page: the three buttons for
that one question, which swap themselves in place. That is what makes "marking
from the list does not reload the list or lose scroll position" a property of the
response rather than a hope about the browser -- the list is not in the response
at all.

There is no CSRF token, and that is a decision rather than an omission: there is
no session, no cookie and no auth to forge against, and the server binds loopback
(008). If D1 ever chooses the LAN option, this endpoint is one of the things that
has to change, which is why it is written down here.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from examkb.services import marks as marks_service
from examkb.web.pages import database_url, fragment

router = APIRouter()


@router.post("/marks/{question_id:path}", response_class=HTMLResponse, name="toggle_mark")
def toggle_mark(
    request: Request,
    question_id: str,
    value: str = Query(..., description="known, unsure or flagged."),
) -> HTMLResponse:
    """Set the mark, or clear it if the question already has that one."""
    if value not in marks_service.VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"{value!r} is not a mark; expected one of {', '.join(marks_service.VALUES)}",
        )
    state = marks_service.apply_toggle(question_id, value, url=database_url(request))
    return fragment(
        request, "_marks.html", question_id=question_id, mark=state.value,
        mark_values=marks_service.VALUES,
    )
