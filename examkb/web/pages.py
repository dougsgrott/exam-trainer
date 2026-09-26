"""The Jinja2 environment and the one function every route renders through.

`render()` exists so that the things on every page -- the nav, the stale-projection
banner, the version in the footer -- are computed in one place instead of in each
route's context dict, where the second route to forget one is the one that ships a
page with no warning on it.

Undefined names are errors (`StrictUndefined`). A typo in a template is a 500 in
development rather than a blank space in a page a person is trying to study from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import jinja2
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from examkb import __version__, status as status_module
from examkb.services import marks as marks_service
from examkb.web import markdown, nav

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Built by hand rather than left to `Jinja2Templates(directory=...)`, which builds
# an environment with autoescape guessed and `Undefined` permissive. Both matter:
# this app renders vendor text straight out of the corpus, and a mistyped variable
# should be a 500 in front of the person writing the template, not a blank space in
# a page somebody is trying to study from.
environment = jinja2.Environment(
    loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
    autoescape=jinja2.select_autoescape(default_for_string=True, default=True),
    undefined=jinja2.StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
templates = Jinja2Templates(env=environment)

# The corpus subset renderer, as a filter. `{{ text | md }}` is the only way a
# template is allowed to turn Markdown into HTML, and it raises rather than
# rendering anything outside the subset -- see `examkb/web/markdown.py`.
environment.filters["md"] = markdown.render
environment.filters["plain"] = markdown.plain

# The three values a person can mark with, so `_marks.html` does not have to be
# handed them by every include site.
environment.globals["MARK_VALUES"] = marks_service.VALUES


def database_url(request: Request) -> str | None:
    """The database this request reads, or None for the configured one.

    The second seam `create_app` offers, beside `status_provider`: it is how a test
    points the pages at a copy in its own `tmp_path` without touching the process's
    settings or the engine cache.
    """
    return getattr(request.app.state, "database_url", None)


def url_with(request: Request, changes: dict[str, Any] | None = None, **overrides: Any) -> str:
    """This URL with some query parameters changed, added or removed.

    Takes a dict as well as keywords because a template building a facet link has
    the parameter's *name* in a variable (`url_with({facet.param: value})`), which
    keyword arguments cannot express.

    `page` is dropped unless it is one of the changes: changing a filter while
    staying on page 7 of the old result set shows an empty list, which reads as a
    bug rather than as what it is.
    """
    params = {key: value for key, value in request.query_params.items() if key != "page"}
    for key, value in {**(changes or {}), **overrides}.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = str(value)
    query = urlencode(params)
    return f"{request.url.path}?{query}" if query else request.url.path


def projection_status(request: Request) -> status_module.ProjectionStatus:
    """The status for this request.

    Taken from `app.state` when something put it there -- `create_app(status=...)`
    is how a test points the banner at its own database -- and computed against the
    configured database and corpus otherwise.
    """
    override = getattr(request.app.state, "status_provider", None)
    if override is not None:
        return override()
    return status_module.projection_status()


def base_context(request: Request) -> dict[str, Any]:
    return {
        "request": request,
        "nav": nav.items(request.app),
        "path": request.url.path,
        "status": projection_status(request),
        "url_with": lambda changes=None, **overrides: url_with(request, changes, **overrides),
        "version": __version__,
    }


def fragment(
    request: Request, template: str, *, status_code: int = 200, **context: Any
) -> HTMLResponse:
    """Render a partial: the template, its own context, and nothing else.

    Deliberately not `render()`. A fragment has no nav, no footer and no banner,
    so building the base context for one would mean asking the database how stale
    the projection is every time somebody clicks a button -- and then throwing the
    answer away. `request` is passed because Starlette's template response wants
    it, not because the fragment uses it.
    """
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={"request": request, **context},
        status_code=status_code,
    )


def render(
    request: Request, template: str, *, status_code: int = 200, **context: Any
) -> HTMLResponse:
    """Render `template` with the base context plus whatever the route adds."""
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={**base_context(request), **context},
        status_code=status_code,
    )
