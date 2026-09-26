"""The nav bar, and which of its entries are real yet.

The skeleton exists before most of its pages do, so half this list points at routes
that land in a later issue. An entry no route serves is rendered as disabled text
naming that issue, not as a link that 404s.

"Does a route serve it" is asked of the running app, by **route name**, through
`app.url_path_for`. Two reasons it is the name and not the path:

- The app answers, so 010 registering `name="browse"` lights the entry up with no
  edit to this file and no chance of the nav and the router disagreeing.
- `url_path_for` is the framework's own question. Walking `app.routes` looking for
  a string works until the framework nests its routers, which this one does.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from starlette.routing import NoMatchFound


@dataclass(frozen=True)
class NavItem:
    label: str
    route: str
    """The route's `name=`. 010 owns `browse`, 015 `exams`, 022 `progress`."""

    issue: str | None = None
    """The issue that lands this page. None for one that already exists."""

    path: str | None = None
    """Filled in by `items()` once the app has resolved it. None when unbuilt."""

    @property
    def enabled(self) -> bool:
        return self.path is not None


NAV: tuple[NavItem, ...] = (
    NavItem("Home", "home"),
    NavItem("Browse", "browse", issue="010"),
    NavItem("Exams", "exams", issue="015"),
    NavItem("Progress", "progress", issue="022"),
)


def resolve(app: Any, route: str) -> str | None:
    """The URL for a named route, or None when nothing serves it."""
    try:
        return str(app.url_path_for(route))
    except NoMatchFound:
        return None


def items(app: Any) -> list[NavItem]:
    """`NAV`, each entry carrying its URL iff the app actually serves it."""
    return [replace(item, path=resolve(app, item.route)) for item in NAV]
