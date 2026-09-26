"""The FastAPI application.

Three things happen here and nothing else: static files are mounted from the
directory they are vendored into, the routers register themselves, and errors get
a page instead of a stack trace. Pages live in `routes/`, queries live in
`examkb/queries.py`, and neither this module nor anything below it imports
SQLAlchemy -- `tests/test_layering.py` is what keeps that true.

There is no CDN and no build step. Every byte the browser fetches comes out of
`examkb/web/static/`, whose provenance is recorded in `VENDOR.json`; the app
refuses to start if one of those files has gone missing, because a page that
silently loses HTMX looks like a page whose buttons stopped working.

There is no module-level `app` on purpose. `uvicorn examkb.web.app:app` would
bind whatever uvicorn's own default is, which is loopback today and is not a
promise anybody made; the only supported way to run this is `examkb serve`,
which binds the socket itself and checks the address it got (`serve.py`).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from examkb import __version__
from examkb.web import routes
from examkb.web.pages import STATIC_DIR, render

VENDOR_MANIFEST = STATIC_DIR / "VENDOR.json"

ERROR_TITLES = {
    404: "Not found",
    405: "Not allowed here",
    500: "Something broke",
}


def vendored_assets() -> list[dict[str, Any]]:
    """Every third-party file this app serves, as recorded in `VENDOR.json`."""
    manifest = json.loads(VENDOR_MANIFEST.read_text(encoding="utf-8"))
    return list(manifest["assets"])


def check_assets() -> None:
    missing = [
        asset["file"] for asset in vendored_assets() if not (STATIC_DIR / asset["file"]).is_file()
    ]
    if missing:
        raise RuntimeError(
            "vendored assets are missing from examkb/web/static: "
            + ", ".join(missing)
            + " -- see VENDOR.json for where each one came from"
        )


def create_app(
    *,
    status_provider: Callable[[], Any] | None = None,
    database_url: str | None = None,
) -> FastAPI:
    """Build the application.

    Two seams, and only two. `status_provider` replaces "ask the configured
    database how stale it is" with "ask this database", so the banner can be driven
    through all five of its states without a global. `database_url` does the same
    for the pages, so a test renders `/browse` against a copy in its own `tmp_path`
    rather than against whatever this checkout happens to have ingested.
    """
    check_assets()

    app = FastAPI(
        title="examkb",
        version=__version__,
        docs_url=None,  # No interactive docs: this is a study app, not an API product.
        redoc_url=None,
        openapi_url=None,
    )
    app.state.status_provider = status_provider
    app.state.database_url = database_url

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    routes.include_all(app)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        """Liveness, and nothing else.

        Deliberately does not touch the database: an ingest holding the write lock
        must not be able to make this endpoint report the server as down. What the
        database holds is the home page's banner, which is a different question.
        """
        return JSONResponse({"status": "ok", "version": __version__})

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        return _error_page(request, exc.status_code, exc.detail)

    @app.exception_handler(Exception)
    def unhandled_error(request: Request, exc: Exception) -> Response:
        # Starlette re-raises after this so the traceback still reaches the log;
        # what changes is what the browser gets.
        return _error_page(request, 500, type(exc).__name__)

    return app


def _error_page(request: Request, status_code: int, detail: Any) -> Response:
    title = ERROR_TITLES.get(status_code, "Error")
    if request.url.path.startswith("/static") or request.url.path == "/healthz":
        return JSONResponse({"error": title, "detail": str(detail)}, status_code=status_code)
    try:
        return render(
            request,
            "error.html",
            status_code=status_code,
            code=status_code,
            title=title,
            detail=str(detail),
        )
    except Exception:  # pragma: no cover -- the error page itself is broken
        return HTMLResponse(f"<h1>{status_code} {title}</h1>", status_code=status_code)

