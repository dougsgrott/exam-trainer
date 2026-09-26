"""The pages: what renders, what the banner says, and what an error looks like.

The banner is the point of this file. A projection is a cache, and a cache that
has quietly fallen behind its source is worse than no cache at all, because every
number on every page is then confidently wrong. So the banner is driven through
all five states it can be in -- no corpus, no database, no schema, nothing
ingested, stale -- with a real database and a real corpus in `tmp_path` for each,
rather than by asserting a string against a hand-built status object.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import write_kb
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from examkb import db as db_module
from examkb import status as status_module
from examkb.ingest import ingest
from examkb.web import nav
from examkb.web.app import create_app


@pytest.fixture(autouse=True)
def clean_status_caches():
    """The corpus fingerprint and the engine are cached by design; not across tests."""
    status_module.forget_corpus_fingerprint()
    db_module.engine_for.cache_clear()
    db_module.forget_projection_ready()
    yield
    status_module.forget_corpus_fingerprint()
    db_module.engine_for.cache_clear()
    db_module.forget_projection_ready()


def provider_for(url: str, kb: Path):
    """The seam `create_app` offers: ask *this* database how stale it is."""
    return lambda: status_module.projection_status(url=url, kb=kb)


def client_for(url: str, kb: Path, **kwargs) -> TestClient:
    """A client whose banner looks at the database and corpus a test just built.

    Both arguments are required, deliberately. A client built against the
    configured database would read the repo's own `examkb.db` and create its WAL
    files beside it -- which `repo_is_untouched` catches, but only after the test
    has already touched the thing it was not supposed to touch.
    """
    return TestClient(create_app(status_provider=provider_for(url, kb)), **kwargs)


@pytest.fixture
def nowhere(tmp_path: Path) -> tuple[str, Path]:
    """A database and a corpus that do not exist. Enough for any page-level test."""
    return f"sqlite:///{tmp_path / 'none.db'}", tmp_path / "none-kb"


@pytest.fixture
def client(nowhere) -> TestClient:
    return client_for(*nowhere)


@pytest.fixture
def ingested(tmp_db, tmp_kb: Path) -> tuple[str, Path]:
    """A migrated database with `tmp_kb`'s three questions projected into it."""
    with Session(tmp_db, expire_on_commit=False) as session:
        ingest(session, tmp_kb)
        session.commit()
    return str(tmp_db.url), tmp_kb


# --------------------------------------------------------------------- the plumbing


def test_healthz_is_alive_and_touches_nothing(client: TestClient) -> None:
    """Liveness must answer while an ingest holds the write lock."""
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]


def test_healthz_answers_with_no_database_at_all(tmp_path: Path) -> None:
    missing = f"sqlite:///{tmp_path / 'nope.db'}"
    assert client_for(missing, tmp_path / "nokb").get("/healthz").status_code == 200
    assert not (tmp_path / "nope.db").exists(), "asking about health created a database"


def test_static_files_are_served_from_disk(client: TestClient) -> None:
    css = client.get("/static/app.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    htmx = client.get("/static/htmx.min.js")
    assert htmx.status_code == 200
    assert b"htmx" in htmx.content[:200]


def test_there_are_no_api_docs_endpoints(client: TestClient) -> None:
    """This is a study app, not an API product; an open schema endpoint is surface."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# ------------------------------------------------------------------------ the layout


def test_the_home_page_renders_the_corpus_counts(ingested) -> None:
    url, kb = ingested
    body = client_for(url, kb).get("/").text
    assert "The corpus" in body
    assert ">3<" in body, "three questions from tmp_kb"


def test_every_page_carries_the_nav_and_the_footer(ingested) -> None:
    url, kb = ingested
    body = client_for(url, kb).get("/").text
    for item in nav.NAV:
        assert item.label in body
    assert "examkb" in body


def test_the_nav_links_only_to_pages_that_exist(client: TestClient) -> None:
    """An entry for an unbuilt page is named and greyed, never a link that 404s."""
    body = client.get("/").text
    for item in nav.items(client.app):
        if item.enabled:
            assert f'href="{item.path}"' in body
        else:
            assert item.issue, f"{item.label} is unbuilt and does not name its issue"
            assert f'data-issue="{item.issue}"' in body
            # And the promise the nav is making is true: nothing serves it.
            assert client.get(f"/{item.route}").status_code == 404


def test_every_nav_entry_is_either_served_or_owned_by_an_issue(client: TestClient) -> None:
    """No third state. A dead entry is a link to nowhere with nobody to blame."""
    for item in nav.items(client.app):
        assert item.enabled or item.issue


def test_a_nav_entry_lights_up_when_a_route_appears(nowhere) -> None:
    """010 registering `/browse` should need no edit to nav.py."""
    from fastapi import APIRouter

    app = create_app(status_provider=provider_for(*nowhere))
    router = APIRouter()
    router.add_api_route("/browse", lambda: "later", methods=["GET"], name="browse")
    app.include_router(router)

    body = TestClient(app).get("/").text
    assert 'href="/browse"' in body
    assert 'data-issue="010"' not in body


def test_the_current_page_is_marked_in_the_nav(client: TestClient) -> None:
    assert 'aria-current="page"' in client.get("/").text


# ------------------------------------------------------------------------ the banner


def test_no_banner_when_the_projection_is_current(ingested) -> None:
    url, kb = ingested
    body = client_for(url, kb).get("/").text
    assert 'class="banner' not in body


def test_banner_when_there_is_no_corpus(tmp_path: Path, tmp_db) -> None:
    body = client_for(str(tmp_db.url), tmp_path / "absent-kb").get("/").text
    assert 'class="banner' in body
    assert "No corpus" in body
    assert "make pipeline" in body


def test_banner_when_there_is_no_database(tmp_path: Path, tmp_kb: Path) -> None:
    body = client_for(f"sqlite:///{tmp_path / 'absent.db'}", tmp_kb).get("/").text
    assert "No database yet" in body
    assert "examkb db upgrade" in body
    assert not (tmp_path / "absent.db").exists(), "rendering a page created a database"


def test_banner_when_the_database_has_no_schema(tmp_path: Path, tmp_kb: Path) -> None:
    """An empty file is a valid SQLite database with no tables in it."""
    empty = tmp_path / "empty.db"
    empty.touch()
    body = client_for(f"sqlite:///{empty}", tmp_kb).get("/").text
    assert "no schema" in body
    assert "examkb db upgrade" in body


def test_banner_when_nothing_has_been_ingested(tmp_db, tmp_kb: Path) -> None:
    body = client_for(str(tmp_db.url), tmp_kb).get("/").text
    assert "Nothing ingested yet" in body
    assert "examkb ingest" in body


def test_banner_when_the_projection_is_older_than_the_corpus(
    ingested, mini_questions: list[dict]
) -> None:
    """The criterion: a projection older than the current `kb` fingerprint warns.

    The corpus is changed after the ingest, exactly as it would be by re-running
    the pipeline, and the page must say so rather than showing stale counts as if
    they were current.
    """
    url, kb = ingested
    assert 'class="banner' not in client_for(url, kb).get("/").text

    write_kb(kb, mini_questions[:2])  # one question leaves the corpus
    status_module.forget_corpus_fingerprint()

    body = client_for(url, kb).get("/").text
    assert 'class="banner' in body
    assert "older than kb/" in body
    assert "examkb ingest" in body
    # The stale numbers are still shown -- with the warning, not instead of it.
    assert ">3<" in body


def test_the_footer_names_the_corpus_the_page_was_built_from(ingested) -> None:
    url, kb = ingested
    status = status_module.projection_status(url=url, kb=kb)
    body = client_for(url, kb).get("/").text
    assert status.short in body
    assert len(status.short) == 12


# ------------------------------------------------------------------------- the errors


def test_an_unknown_page_gets_a_page_not_a_traceback(client: TestClient) -> None:
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Not found" in response.text
    assert "Traceback" not in response.text


def test_the_error_page_still_offers_the_way_back(client: TestClient) -> None:
    assert 'href="/"' in client.get("/no-such-page").text


def test_an_unhandled_exception_renders_500_without_leaking_the_traceback(nowhere) -> None:
    from fastapi import APIRouter

    def boom():
        raise RuntimeError("the database caught fire")

    app = create_app(status_provider=provider_for(*nowhere))
    router = APIRouter()
    router.add_api_route("/boom", boom, methods=["GET"])
    app.include_router(router)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/boom")
    assert response.status_code == 500
    assert "Something broke" in response.text
    assert "the database caught fire" not in response.text
    assert "Traceback" not in response.text


def test_a_missing_static_file_is_json_not_a_page(client: TestClient) -> None:
    """A 404 on an asset should not cost a full HTML render in the browser's console."""
    response = client.get("/static/nope.css")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


# --------------------------------------------------------------- the status itself


def test_status_states_are_distinct_and_ordered(tmp_path: Path, tmp_kb: Path, tmp_db) -> None:
    """Walk the whole ladder once, in one test, so the order is visible in one place."""
    seen = []

    seen.append(status_module.projection_status(url=str(tmp_db.url), kb=tmp_path / "gone").state)
    seen.append(
        status_module.projection_status(url=f"sqlite:///{tmp_path / 'x.db'}", kb=tmp_kb).state
    )
    empty = tmp_path / "empty.db"
    empty.touch()
    status_module.forget_corpus_fingerprint()
    seen.append(status_module.projection_status(url=f"sqlite:///{empty}", kb=tmp_kb).state)
    seen.append(status_module.projection_status(url=str(tmp_db.url), kb=tmp_kb).state)

    with Session(tmp_db, expire_on_commit=False) as session:
        ingest(session, tmp_kb)
        session.commit()
    seen.append(status_module.projection_status(url=str(tmp_db.url), kb=tmp_kb).state)

    assert seen == [
        status_module.NO_CORPUS,
        status_module.NO_DATABASE,
        status_module.NOT_MIGRATED,
        status_module.NOT_INGESTED,
        status_module.READY,
    ]


def test_the_corpus_fingerprint_is_cached_against_the_shard_index(tmp_kb: Path, monkeypatch) -> None:
    """A banner that re-hashed the corpus on every request is a banner somebody deletes.

    Counts reads of `corpus_rows`, which is what 018 made this hash: shards *and*
    blueprints, the same list ingest stores its fingerprint from.
    """
    calls = []
    real = status_module.ingest.corpus_rows
    monkeypatch.setattr(
        status_module.ingest, "corpus_rows", lambda kb: (calls.append(kb), real(kb))[1]
    )

    first = status_module.corpus_fingerprint(tmp_kb)
    for _ in range(50):
        assert status_module.corpus_fingerprint(tmp_kb) == first
    assert len(calls) == 1, "the fingerprint was recomputed on a request"


def test_the_cache_notices_when_the_corpus_changes(tmp_kb: Path, mini_questions) -> None:
    before = status_module.corpus_fingerprint(tmp_kb)
    write_kb(tmp_kb, mini_questions[:1])
    assert status_module.corpus_fingerprint(tmp_kb) != before
