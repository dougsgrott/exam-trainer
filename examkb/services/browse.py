"""Page assembly for `/browse` and `/questions/{id}`.

This module is the session seam 008 deliberately left until there was a query to
run. It lives outside `examkb/web/` for the reason the layering rule exists: a
route that holds a `Session` is one refactor away from holding a `select()`, and
`tests/test_layering.py` forbids `examkb.db` under `web/` precisely so that the
temptation never arrives. So the web layer asks for a *page* and gets a frozen
dataclass; the transaction opens and closes in here.

One session per page, and a fixed number of statements inside it -- two for the
results (count and page) plus one per facet plus, on the detail page, three. None
of them depends on the page size, which is the acceptance criterion that keeps a
template from reaching back into the database while it renders.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from examkb import db, queries
from examkb.queries import QuestionDetail
from examkb.services import search
from examkb.services import search
from examkb.services.search import Facet, SearchFilters, SearchResults

DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 100

# The URL parameter each filter reads, and the `SearchFilters` field it sets. One
# table so the route, the facets and the links cannot disagree about a spelling.
FILTER_PARAMS: dict[str, str] = {
    "cert": "certification_id",
    "exam": "exam_id",
    "mode": "exam_mode",
    "domain": "domain_label",
    "type": "type",
    "mark": "mark",
}


@dataclass(frozen=True)
class BrowsePage:
    """Everything `/browse` renders, and nothing that needs a database to read."""

    results: SearchResults
    facets: list[Facet]
    q: str
    page: int
    per_page: int
    active: dict[str, str]
    """The filter parameters actually in force, as they appear in the URL."""

    @property
    def total(self) -> int:
        return self.results.total

    @property
    def pages(self) -> int:
        return max(1, ceil(self.total / self.per_page)) if self.per_page else 1

    @property
    def first(self) -> int:
        """1-based index of the first row on this page; 0 when there are none."""
        return 0 if not self.total else (self.page - 1) * self.per_page + 1

    @property
    def last(self) -> int:
        return min(self.page * self.per_page, self.total)

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def filtered(self) -> bool:
        return bool(self.active) or bool(self.q.strip())


def filters_from(active: dict[str, str]) -> SearchFilters:
    """URL parameters -> `SearchFilters`. Unknown parameters are ignored, not errors."""
    return SearchFilters(
        **{
            FILTER_PARAMS[param]: value
            for param, value in active.items()
            if param in FILTER_PARAMS and value
        }
    )


def clean(params: dict[str, str | None]) -> dict[str, str]:
    """The filter parameters that are actually set, in `FILTER_PARAMS` order."""
    return {
        param: params[param]
        for param in FILTER_PARAMS
        if params.get(param)
    }


def browse_page(
    *,
    q: str = "",
    params: dict[str, str | None] | None = None,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
    url: str | None = None,
) -> BrowsePage:
    """The list, its counts and its facets, from one session.

    `page` and `per_page` are clamped rather than validated: `?page=0` and
    `?page=99999` are things people type and things crawlers invent, and neither
    deserves a 422 on a page whose whole job is to show a list.
    """
    active = clean(params or {})
    filters = filters_from(active)
    per_page = max(1, min(int(per_page or DEFAULT_PER_PAGE), MAX_PER_PAGE))
    page = max(1, int(page or 1))

    if not db.projection_ready(url):
        # Before the first `db upgrade` there is nothing to query. The banner (008)
        # already says which command is missing; this just declines to shout.
        return BrowsePage(
            results=SearchResults(query=search.parse(q), total=0),
            facets=[],
            q=q,
            page=1,
            per_page=per_page,
            active=active,
        )

    with db.session_for(url) as session:
        results = search.search_questions(
            session, q, filters=filters, limit=per_page, offset=(page - 1) * per_page
        )
        # Asking for page 900 of 22 should show the last page, not an empty one.
        pages = max(1, ceil(results.total / per_page))
        if page > pages and results.total:
            page = pages
            results = search.search_questions(
                session, q, filters=filters, limit=per_page, offset=(page - 1) * per_page
            )
        found = search.facets(session, q, filters=filters)

    return BrowsePage(
        results=results, facets=found, q=q, page=page, per_page=per_page, active=active
    )


def question_page(question_id: str, *, url: str | None = None) -> QuestionDetail | None:
    """One question, or None. The route turns None into a 404."""
    if not db.projection_ready(url):
        return None
    with db.session_for(url) as session:
        return queries.question_detail(session, question_id)
