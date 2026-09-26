"""The route registry, mirroring the CLI's subcommand registry.

Adding a page is a new module under this package plus one import in `ROUTERS` --
never an edit to `app.py`'s wiring. Each entry names the issue that owns it, so a
reader can go from a URL to the issue that specified it without grepping.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from examkb.web.routes import browse, exam, home, marks, results

ROUTERS: tuple[APIRouter, ...] = (
    home.router,  # 008
    browse.router,  # 010
    marks.router,  # 011
    exam.router,  # 015
    results.router,  # 016
)


def include_all(app: FastAPI) -> None:
    for router in ROUTERS:
        app.include_router(router)
