"""`/browse` and the question detail page.

Three things are being defended here, and only one of them is visible on the page.

**The counts add up.** A facet count that disagrees with the result count is worse
than no count, because it is confidently wrong. Every dimension is counted over
the same scope as the results, so each one sums to the filtered total -- including
the 189 questions whose exam the vendor never labelled, which is the bucket a naive
`GROUP BY exam.mode` silently drops.

**The page size does not depend on the corpus size.** That is the whole reason this
replaces a 3 MB static file, and it is asserted twice: as a bounded statement count
whatever the page size, and as a response body measured in kilobytes.

**Nothing renders outside the Markdown subset.** `test_markdown.py` owns the rule;
what is asserted here is that the detail page actually applies it, to all 549.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine, event
from starlette.testclient import TestClient

from examkb import db as db_module
from examkb import status as status_module
from examkb.services import browse as browse_service
from examkb.services.search import FACETS, UNSPECIFIED, SearchFilters
from examkb.web.app import create_app

CCAO_DOMAIN = "Output Evaluation and Validation"
CCAR_DOMAIN = "Developer Productivity & Operational Enablement"
CORPUS = 549


@pytest.fixture(autouse=True)
def clean_caches():
    db_module.engine_for.cache_clear()
    db_module.forget_projection_ready()
    status_module.forget_corpus_fingerprint()
    yield
    db_module.engine_for.cache_clear()
    db_module.forget_projection_ready()
    status_module.forget_corpus_fingerprint()


@pytest.fixture
def url(real_db: Engine) -> str:
    """The real 549-question projection, in this test's own `tmp_path`."""
    return str(real_db.url)


@pytest.fixture
def client(url: str, real_db: Engine) -> TestClient:
    app = create_app(
        database_url=url,
        status_provider=lambda: status_module.projection_status(url=url),
    )
    return TestClient(app)


def page(url: str, **kwargs) -> browse_service.BrowsePage:
    return browse_service.browse_page(url=url, **kwargs)


def facet(result: browse_service.BrowsePage, name: str):
    return next(found for found in result.facets if found.name == name)


def count_of(result: browse_service.BrowsePage, name: str, value: str) -> int:
    return next(v.count for v in facet(result, name).values if v.value == value)


# ------------------------------------------------------------------------ the counts


def test_the_domain_filter_returns_the_counted_numbers(url: str) -> None:
    """The criterion, in the vendor's own strings."""
    assert page(url, params={"domain": CCAO_DOMAIN}).total == 78
    assert page(url, params={"domain": CCAR_DOMAIN}).total == 12


def test_those_numbers_also_appear_as_facet_counts(url: str) -> None:
    unfiltered = page(url)
    assert count_of(unfiltered, "domain", CCAO_DOMAIN) == 78
    assert count_of(unfiltered, "domain", CCAR_DOMAIN) == 12


def test_every_facet_sums_to_the_corpus_when_nothing_is_filtered(url: str) -> None:
    result = page(url)
    assert result.total == CORPUS
    for found in result.facets:
        assert found.total == CORPUS, f"{found.name} sums to {found.total}, not {CORPUS}"


@pytest.mark.parametrize(
    "params",
    [
        {"domain": CCAO_DOMAIN},
        {"cert": "ccao-f"},
        {"type": "multi_select"},
        {"mode": "hard"},
        {"mode": UNSPECIFIED},
        {"cert": "ccar-p", "type": "single_select"},
        {"exam": "ccao-f/exam-01"},
    ],
)
def test_every_facet_sums_to_the_filtered_total(url: str, params: dict) -> None:
    result = page(url, params=params)
    assert result.total > 0
    for found in result.facets:
        assert found.total == result.total, f"{found.name}: {found.total} != {result.total}"


def test_the_facets_agree_with_the_results_when_a_search_is_on_too(url: str) -> None:
    result = page(url, q="evaluation", params={"domain": CCAO_DOMAIN})
    assert 0 < result.total < 78
    for found in result.facets:
        assert found.total == result.total


def test_the_mode_facet_keeps_the_questions_the_vendor_never_labelled(url: str) -> None:
    """189 of 549 sit in exams with no mode. Drop them and the total reads 360."""
    mode = facet(page(url), "mode")
    values = {value.value: value.count for value in mode.values}

    assert values[UNSPECIFIED] == 189
    assert values["hard"] == 180 and values["realistic"] == 180
    assert sum(values.values()) == CORPUS
    assert sum(count for value, count in values.items() if value != UNSPECIFIED) == 360


def test_filtering_for_the_unlabelled_mode_works(url: str) -> None:
    assert page(url, params={"mode": UNSPECIFIED}).total == 189


def test_no_real_facet_value_collides_with_the_sentinel(real_db: Engine) -> None:
    """`-` is only safe as "not stated" while nothing real is ever spelled that way.

    Asked of the columns rather than of the rendered facets: a facet that folded a
    genuine `-` into its "not stated" bucket would look perfectly correct here and
    be wrong in the database, which is the direction this needs to be checked from.
    """
    columns = [
        ("question", "certification_id"),
        ("question", "exam_id"),
        ("question", "domain_label"),
        ("question", "type"),
        ("exam", "mode"),
        ("mark", "value"),
    ]
    with real_db.connect() as connection:
        for table, column in columns:
            found = connection.exec_driver_sql(
                f"SELECT count(*) FROM {table} WHERE {column} = '{UNSPECIFIED}'"
            ).scalar()
            assert found == 0, f"{table}.{column} contains a literal {UNSPECIFIED!r}"


def test_the_selected_value_is_marked_in_its_own_facet(url: str) -> None:
    result = page(url, params={"domain": CCAO_DOMAIN})
    domain = facet(result, "domain")
    assert domain.selected == CCAO_DOMAIN
    assert [value.value for value in domain.values if value.selected] == [CCAO_DOMAIN]


# -------------------------------------------------------------- bounded, not N+1


# Transaction control is not a query. `examkb/db.py` emits its own `BEGIN` (005's
# pysqlite recipe), and counting it would make this test about SQLAlchemy's
# bookkeeping rather than about how many times the page hits the corpus.
NOT_A_QUERY = ("BEGIN", "COMMIT", "ROLLBACK", "PRAGMA")


def statements(engine: Engine) -> list[str]:
    """Every real statement run on `engine`, appended as it happens."""
    seen: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def record(_conn, _cursor, statement, *_args):  # noqa: ANN001
        if not statement.lstrip().upper().startswith(NOT_A_QUERY):
            seen.append(statement)

    return seen


def test_the_list_page_issues_the_same_queries_whatever_the_page_size(
    url: str, real_db: Engine
) -> None:
    """The criterion: bounded queries regardless of page size -- no N+1.

    Two for the results (the count and the page) plus one per facet. A template
    that reached back for an exam title would turn the second number into the page
    size, which is exactly what the 3 MB file this replaces was avoiding by
    shipping everything at once.
    """
    engine = db_module.engine_for(url)
    # The schema check (051) happens once per process, not once per page, so it is
    # warmed before the listener goes on -- this counts what rendering costs, not
    # what starting up costs.
    db_module.projection_ready(url)
    seen = statements(engine)

    page(url, per_page=5)
    small = len(seen)
    seen.clear()
    page(url, per_page=100)
    large = len(seen)

    assert small == large == 2 + len(FACETS)


def test_the_detail_page_issues_three_queries(url: str) -> None:
    engine = db_module.engine_for(url)
    # The schema check (051) happens once per process, not once per page, so it is
    # warmed before the listener goes on -- this counts what rendering costs, not
    # what starting up costs.
    db_module.projection_ready(url)
    seen = statements(engine)

    detail = browse_service.question_page("ccao-f/exam-01/q001", url=url)

    assert detail is not None
    assert len(seen) == 3  # question, options, references


def test_the_page_is_kilobytes_not_megabytes(client: TestClient) -> None:
    """`kb/reports/browse.html` is 3 MB because it inlines all 549 questions."""
    body = client.get("/browse").content
    assert len(body) < 100_000
    assert len(client.get("/browse?per_page=100").content) < 400_000


def test_a_huge_page_size_is_clamped_rather_than_honoured(url: str) -> None:
    assert page(url, per_page=100_000).per_page == browse_service.MAX_PER_PAGE


# ---------------------------------------------------------------------- the pages


def test_browse_renders_with_its_filters_and_counts(client: TestClient) -> None:
    body = client.get("/browse").text
    assert "Browse" in body
    assert str(CORPUS) in body
    for spec in FACETS:
        assert spec.title in body


def test_a_filter_is_a_link_somebody_can_send(client: TestClient) -> None:
    """Filter state lives in the URL, so a view is linkable and Back works."""
    response = client.get(f"/browse?domain={CCAO_DOMAIN}")
    assert response.status_code == 200
    assert "78" in response.text
    # And the same URL renders the same thing again.
    assert client.get(f"/browse?domain={CCAO_DOMAIN}").text == response.text


def test_choosing_a_filter_returns_to_page_one(client: TestClient) -> None:
    """A filter link from page 7 that kept `page=7` would land on an empty list."""
    body = client.get("/browse?page=7").text
    assert "page=8" in body  # the pager still moves
    assert "page=7" not in body.split('class="pager"')[0], "a facet link carried the page over"


def test_paging_moves_through_the_corpus(client: TestClient) -> None:
    first = client.get("/browse?per_page=10").text
    second = client.get("/browse?per_page=10&page=2").text
    assert first != second
    assert "Page 2 of" in second


def test_a_page_past_the_end_shows_the_last_page(url: str) -> None:
    result = page(url, page=9999, per_page=25)
    assert result.page == result.pages == 22
    assert result.results.hits


def test_the_search_box_composes_with_the_filters(client: TestClient) -> None:
    response = client.get("/browse?q=caching&cert=ccar-p")
    assert response.status_code == 200
    assert "<mark>" in response.text


def test_the_question_id_survives_being_a_path(client: TestClient) -> None:
    """`ccao-f/exam-01/q001` is one identifier, not three path segments."""
    response = client.get("/questions/ccao-f/exam-01/q001")
    assert response.status_code == 200
    assert "ccao-f/exam-01/q001" in response.text


def test_the_detail_page_shows_the_question_and_its_answer(client: TestClient) -> None:
    body = client.get("/questions/ccao-f/exam-01/q001").text
    assert "Options" in body and "correct" in body
    assert "Explanation" in body
    assert "References" in body
    assert "multi select" in body


def test_an_unknown_question_is_a_404_page(client: TestClient) -> None:
    response = client.get("/questions/nope/exam-99/q999")
    assert response.status_code == 404
    assert "Not found" in response.text


def test_the_nav_now_links_to_browse(client: TestClient) -> None:
    """008's nav asks the app; 010 registering `name="browse"` is the whole change."""
    body = client.get("/").text
    assert 'href="/browse"' in body
    assert 'data-issue="010"' not in body


def test_every_question_renders_its_detail_page(url: str) -> None:
    """The criterion, swept over all 549 through the renderer that can fail.

    The HTTP version of this sweep is the `slow` test below; what can actually
    raise is the Markdown subset check on each field, and that runs here on every
    field of every question.
    """
    from examkb.web.markdown import render

    ids = [hit.question_id for hit in page(url, per_page=browse_service.MAX_PER_PAGE).results.hits]
    for offset in range(100, CORPUS, 100):
        ids += [
            hit.question_id
            for hit in page(url, per_page=100, page=offset // 100 + 1).results.hits
        ]
    assert len(set(ids)) == CORPUS

    for question_id in sorted(set(ids)):
        detail = browse_service.question_page(question_id, url=url)
        assert detail is not None
        render(detail.prompt_md, where=f"{question_id} prompt")
        render(detail.overall_explanation_md, where=f"{question_id} overall")
        assert detail.options
        for option in detail.options:
            render(option.text_md, where=f"{question_id} {option.label}")
            render(option.explanation_md, where=f"{question_id} {option.label} why")
        assert detail.references


@pytest.mark.slow
def test_every_detail_page_answers_over_http(client: TestClient, url: str) -> None:
    """The same sweep through the real stack: 549 requests, 549 rendered templates."""
    ids = sorted(
        hit.question_id
        for number in range(1, 7)
        for hit in page(url, per_page=100, page=number).results.hits
    )
    assert len(ids) == CORPUS

    for question_id in ids:
        response = client.get(f"/questions/{question_id}")
        assert response.status_code == 200, question_id
        assert len(response.content) < 100_000


# ------------------------------------------------------------------- the filter map


def test_the_url_parameters_map_onto_real_filter_fields() -> None:
    """A typo here is a filter that silently does nothing."""
    fields = set(SearchFilters.__dataclass_fields__)
    for param, attribute in browse_service.FILTER_PARAMS.items():
        assert attribute in fields, f"?{param}= sets {attribute}, which does not exist"


def test_every_facet_has_a_url_parameter_that_sets_it() -> None:
    for spec in FACETS:
        assert browse_service.FILTER_PARAMS.get(spec.param) == spec.attribute


def test_unknown_parameters_are_ignored_rather_than_fatal(url: str) -> None:
    assert page(url, params={"nonsense": "x"}).total == CORPUS
