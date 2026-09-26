"""Marks: the first rows in this repo that cannot be rebuilt from anything.

`kb/` regenerates from `data/` and the projection regenerates from `kb/`. A mark
regenerates from nothing, which is why two of the properties here matter more than
the feature does:

**Ingest never touches them.** A rebuild wipes and re-projects every PROJECTION
table; marks sit beside that and must come through byte-identical, including for a
question that has left the corpus entirely.

**Nothing deletes.** Clearing a mark appends a `cleared` row. "Known in March,
unsure in June" is the thing `localStorage['kb-marks']` could not do and the whole
reason this table exists, so a test asserts the history survives every operation
rather than trusting that no `DELETE` gets written later.

The importer gets the rest of the attention. It is the migration path for study
state the user already has, and they will run it more than once.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import REPO_ROOT, write_kb
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from examkb import db as db_module
from examkb import status as status_module
from examkb.ingest import ingest
from examkb.models import journal
from examkb.services import marks as marks_service
from examkb.services.browse import browse_page
from examkb.services.marks import (
    CLEARED,
    MarkError,
    clear_mark,
    counts,
    current,
    history,
    import_marks,
    parse_blob,
    set_mark,
    toggle,
)
from examkb.services.search import UNSPECIFIED
from examkb.web.app import create_app

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
Q1 = "mini-a/exam-01/q001"
Q2 = "mini-a/exam-01/q002"
Q3 = "mini-b/exam-01/q001"


@pytest.fixture(autouse=True)
def clean_caches():
    db_module.engine_for.cache_clear()
    status_module.forget_corpus_fingerprint()
    yield
    db_module.engine_for.cache_clear()
    status_module.forget_corpus_fingerprint()


@pytest.fixture
def mini(tmp_session: Session, tmp_kb: Path) -> Session:
    """The three-question corpus, projected, with an empty journal."""
    ingest(tmp_session, tmp_kb)
    tmp_session.commit()
    return tmp_session


def values(session: Session, question_id: str) -> list[str]:
    return [mark.value for mark in history(session, question_id)]


# ----------------------------------------------------------------- append, never edit


def test_a_mark_is_appended_and_read_back(mini: Session) -> None:
    set_mark(mini, Q1, "known", at=NOW)
    assert current(mini, Q1).value == "known"
    assert current(mini, Q2).value is None
    assert current(mini, Q2).marked is False


def test_changing_a_mark_keeps_the_old_one(mini: Session) -> None:
    """The criterion: known -> unsure leaves both rows queryable."""
    set_mark(mini, Q1, "known", at=NOW)
    set_mark(mini, Q1, "unsure", at=NOW + timedelta(days=90))

    assert values(mini, Q1) == ["known", "unsure"]
    assert current(mini, Q1).value == "unsure"
    assert current(mini, Q1).marked_at == NOW + timedelta(days=90)


def test_clearing_appends_rather_than_deleting(mini: Session) -> None:
    set_mark(mini, Q1, "known", at=NOW)
    clear_mark(mini, Q1)

    assert values(mini, Q1) == ["known", CLEARED]
    assert current(mini, Q1).value is None, "cleared reads as unmarked"
    assert mini.scalar(sa.select(sa.func.count()).select_from(journal.Mark)) == 2


def test_toggling_the_same_value_clears_it(mini: Session) -> None:
    """What the browser did with `delete marks[id]`, without losing the fact."""
    assert toggle(mini, Q1, "known").value == "known"
    assert toggle(mini, Q1, "known").value is None
    assert toggle(mini, Q1, "flagged").value == "flagged"
    assert values(mini, Q1) == ["known", CLEARED, "flagged"]


def test_a_value_outside_the_vocabulary_is_refused(mini: Session) -> None:
    with pytest.raises(MarkError, match="not a mark"):
        set_mark(mini, Q1, "maybe")
    with pytest.raises(MarkError):
        toggle(mini, Q1, CLEARED)  # clearing is not a thing you toggle to


def test_counts_are_per_question_not_per_row(mini: Session) -> None:
    set_mark(mini, Q1, "known", at=NOW)
    set_mark(mini, Q1, "unsure", at=NOW + timedelta(days=1))
    set_mark(mini, Q2, "known", at=NOW)
    set_mark(mini, Q3, "flagged", at=NOW)
    clear_mark(mini, Q3)

    assert counts(mini) == {"known": 1, "unsure": 1, "flagged": 0}


def test_many_marks_are_read_in_one_statement(mini: Session) -> None:
    set_mark(mini, Q1, "known", at=NOW)
    set_mark(mini, Q3, "flagged", at=NOW)
    clear_mark(mini, Q3)

    found = marks_service.current_many(mini, [Q1, Q2, Q3])
    assert found == {Q1: "known"}, "cleared and never-marked are both absent"


# ------------------------------------------------------------------ ingest never looks


def test_marks_survive_a_rebuild(tmp_db: Engine, tmp_kb: Path) -> None:
    """The criterion. Marks are JOURNAL; `--rebuild` empties PROJECTION and only that."""
    with Session(tmp_db, expire_on_commit=False) as session:
        ingest(session, tmp_kb)
        set_mark(session, Q1, "known", at=NOW)
        set_mark(session, Q1, "unsure", at=NOW + timedelta(days=1))
        set_mark(session, Q2, "flagged", at=NOW)
        session.commit()
        before = [
            (mark.id, mark.question_id, mark.value, mark.source, mark.created_at)
            for mark in session.scalars(sa.select(journal.Mark).order_by(journal.Mark.id))
        ]

    with Session(tmp_db, expire_on_commit=False) as session:
        ingest(session, tmp_kb, rebuild=True)
        session.commit()
        after = [
            (mark.id, mark.question_id, mark.value, mark.source, mark.created_at)
            for mark in session.scalars(sa.select(journal.Mark).order_by(journal.Mark.id))
        ]

    assert after == before
    assert len(after) == 3


def test_a_mark_outlives_the_question_it_was_made_on(
    tmp_session: Session, tmp_kb: Path, mini_questions: list[dict]
) -> None:
    """Attempts and marks are history, not a foreign key cascade (005)."""
    ingest(tmp_session, tmp_kb)
    set_mark(tmp_session, Q1, "known", at=NOW)
    tmp_session.commit()

    write_kb(tmp_kb, mini_questions[1:])  # Q1 leaves the corpus
    ingest(tmp_session, tmp_kb)
    tmp_session.commit()

    assert tmp_session.scalar(
        sa.select(sa.func.count()).select_from(journal.Mark).where(journal.Mark.question_id == Q1)
    ) == 1
    assert current(tmp_session, Q1).value == "known"


# ----------------------------------------------------------------------- the importer


def test_the_bare_browser_blob_imports(mini: Session) -> None:
    result = import_marks(mini, json.dumps({Q1: "known", Q2: "flagged"}), at=NOW)

    assert (result.imported, result.skipped, result.unmatched) == (2, 0, [])
    assert current(mini, Q1).value == "known"
    assert current(mini, Q2).value == "flagged"


def test_a_whole_localstorage_dump_imports(mini: Session) -> None:
    """The criterion: what a person can actually get out of devtools.

    `JSON.stringify(localStorage)` is the easy thing to type, and it nests the
    marks as a JSON *string* inside a JSON object.
    """
    dump = json.dumps({"kb-theme": "dark", "kb-marks": json.dumps({Q1: "unsure"})})
    result = import_marks(mini, dump, at=NOW)

    assert result.imported == 1
    assert current(mini, Q1).value == "unsure"


@pytest.mark.parametrize(
    "blob",
    [
        {Q1: "known"},
        {"marks": {Q1: "known"}},
        {"kb-marks": {Q1: "known"}},
        [{"question_id": Q1, "value": "known"}],
        [{"id": Q1, "value": "known"}],
    ],
)
def test_every_shape_an_export_arrives_in(mini: Session, blob) -> None:
    assert parse_blob(blob) == {Q1: "known"}
    assert import_marks(mini, json.dumps(blob), at=NOW).imported == 1


def test_importing_twice_writes_nothing_the_second_time(mini: Session) -> None:
    """The criterion: same state, and the second run reports all-skipped."""
    blob = json.dumps({Q1: "known", Q2: "flagged"})
    first = import_marks(mini, blob, at=NOW)
    mini.commit()
    rows_after_first = mini.scalar(sa.select(sa.func.count()).select_from(journal.Mark))

    second = import_marks(mini, blob, at=NOW + timedelta(days=1))
    mini.commit()

    assert (first.imported, first.skipped) == (2, 0)
    assert (second.imported, second.skipped) == (0, 2)
    assert mini.scalar(sa.select(sa.func.count()).select_from(journal.Mark)) == rows_after_first
    assert "already current" in second.summary()


def test_an_id_that_is_not_in_the_corpus_is_reported(mini: Session) -> None:
    """The criterion: reported, not silently dropped and not an error."""
    result = import_marks(mini, json.dumps({Q1: "known", "gone/exam-99/q001": "unsure"}), at=NOW)

    assert result.imported == 1
    assert result.unmatched == ["gone/exam-99/q001"]
    assert "not in the corpus" in result.summary()
    assert current(mini, "gone/exam-99/q001").value is None


def test_an_unrecognised_value_is_reported_not_written(mini: Session) -> None:
    result = import_marks(mini, json.dumps({Q1: "maybe"}), at=NOW)

    assert result.imported == 0
    assert result.ignored == {Q1: "maybe"}
    assert "unrecognised" in result.summary()


def test_an_empty_value_is_not_a_cleared_mark(mini: Session) -> None:
    """The browser deletes cleared marks; an empty string means "never marked"."""
    result = import_marks(mini, json.dumps({Q1: "", Q2: "known"}), at=NOW)

    assert result.imported == 1
    assert result.total == 1
    assert values(mini, Q1) == []


def test_importing_over_a_different_mark_keeps_the_old_one(mini: Session) -> None:
    set_mark(mini, Q1, "known", at=NOW)
    import_marks(mini, json.dumps({Q1: "unsure"}), at=NOW + timedelta(days=1))

    assert values(mini, Q1) == ["known", "unsure"]
    assert [mark.source for mark in history(mini, Q1)] == ["ui", "import"]


def test_a_blob_that_is_not_json_says_so(mini: Session) -> None:
    with pytest.raises(MarkError, match="not JSON"):
        import_marks(mini, "{not json", at=NOW)
    with pytest.raises(MarkError, match="expected an object"):
        import_marks(mini, "42", at=NOW)


def test_the_file_mtime_becomes_the_timestamp(tmp_path: Path) -> None:
    """`kb-marks` has no timestamps; the file's mtime beats "whenever you ran this"."""
    blob = tmp_path / "kb-marks.json"
    blob.write_text(json.dumps({Q1: "known"}), encoding="utf-8")
    import os

    os.utime(blob, (NOW.timestamp(), NOW.timestamp()))

    text, stamp = marks_service.read_blob(blob)
    assert json.loads(text) == {Q1: "known"}
    assert stamp == NOW


# -------------------------------------------------------------- the browse integration


@pytest.fixture
def marked(real_db: Engine) -> str:
    """The real corpus with three questions marked, in this test's own database."""
    url = str(real_db.url)
    with Session(real_db, expire_on_commit=False) as session:
        set_mark(session, "ccao-f/exam-01/q001", "known", at=NOW)
        set_mark(session, "ccao-f/exam-01/q002", "flagged", at=NOW)
        set_mark(session, "ccao-f/exam-01/q003", "unsure", at=NOW)
        set_mark(session, "ccao-f/exam-01/q004", "known", at=NOW)
        clear_mark(session, "ccao-f/exam-01/q004")  # back to unmarked
        session.commit()
    return url


def test_the_mark_facet_counts_the_unmarked_too(marked: str) -> None:
    """The criterion: six facets, all still summing to 549."""
    result = browse_page(url=marked, per_page=1)
    facet = next(found for found in result.facets if found.name == "mark")
    values_by_key = {value.value: value.count for value in facet.values}

    assert values_by_key == {"known": 1, "flagged": 1, "unsure": 1, UNSPECIFIED: 546}
    assert facet.total == 549
    for found in result.facets:
        assert found.total == 549


def test_filtering_by_mark_returns_those_questions(marked: str) -> None:
    assert browse_page(url=marked, params={"mark": "known"}).total == 1
    assert browse_page(url=marked, params={"mark": UNSPECIFIED}).total == 546
    assert browse_page(url=marked, params={"mark": "flagged"}).results.hits[0].question_id == (
        "ccao-f/exam-01/q002"
    )


def test_a_cleared_mark_filters_as_unmarked(marked: str) -> None:
    """`current_mark` still has the row; the page must not show it as a mark."""
    hit = browse_page(url=marked, params={"exam": "ccao-f/exam-01"}, per_page=100).results.hits
    by_id = {one.question_id: one.mark for one in hit}

    assert by_id["ccao-f/exam-01/q004"] is None
    assert by_id["ccao-f/exam-01/q001"] == "known"


def test_the_list_still_issues_a_bounded_number_of_queries(marked: str) -> None:
    """The mark join must not have turned the list into an N+1."""
    from sqlalchemy import event

    engine = db_module.engine_for(marked)
    seen: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def record(_conn, _cursor, statement, *_args):  # noqa: ANN001
        if not statement.lstrip().upper().startswith(("BEGIN", "COMMIT", "ROLLBACK", "PRAGMA")):
            seen.append(statement)

    browse_page(url=marked, per_page=5)
    small = len(seen)
    seen.clear()
    browse_page(url=marked, per_page=100)

    assert small == len(seen) == 2 + 6  # count, page, six facets


# ------------------------------------------------------------------------ the toggle


@pytest.fixture
def client(real_db: Engine) -> TestClient:
    url = str(real_db.url)
    return TestClient(
        create_app(
            database_url=url,
            status_provider=lambda: status_module.projection_status(url=url),
        )
    )


def test_a_toggle_returns_the_control_and_nothing_else(client: TestClient) -> None:
    """The criterion: marking from the list cannot reload the list or move scroll.

    Asserted on the response rather than in a browser: the list is not in it. The
    swap target is the control group itself, so there is nothing else for HTMX to
    replace.
    """
    response = client.post("/marks/ccao-f/exam-01/q001?value=known")

    assert response.status_code == 200
    body = response.text
    assert "<html" not in body and "<nav" not in body and "class=\"hit\"" not in body
    assert body.count('class="marks"') == 1
    assert 'hx-target="this"' in body and 'hx-swap="outerHTML"' in body
    assert len(response.content) < 1200


def test_the_list_points_its_swap_at_the_control_not_at_itself(client: TestClient) -> None:
    body = client.get("/browse?per_page=3").text
    assert body.count('hx-target="this"') == 3
    assert 'hx-target="#list"' not in body and 'hx-target="body"' not in body


def test_toggling_twice_leaves_the_question_unmarked(client: TestClient) -> None:
    first = client.post("/marks/ccao-f/exam-01/q001?value=known")
    second = client.post("/marks/ccao-f/exam-01/q001?value=known")

    assert 'aria-pressed="true"' in first.text
    assert 'aria-pressed="true"' not in second.text
    assert 'data-mark=""' in second.text


def test_a_mark_is_still_there_after_a_reload(client: TestClient) -> None:
    client.post("/marks/ccao-f/exam-01/q001?value=flagged")

    detail = client.get("/questions/ccao-f/exam-01/q001").text
    assert 'data-mark="flagged"' in detail
    assert "/browse?mark=flagged" in client.get("/browse").text


def test_an_unknown_mark_value_is_a_400(client: TestClient) -> None:
    assert client.post("/marks/ccao-f/exam-01/q001?value=nope").status_code == 400


def test_the_toggle_does_not_pay_for_the_stale_banner(client: TestClient) -> None:
    """A fragment has no nav and no banner, so it must not go asking about them."""
    body = client.post("/marks/ccao-f/exam-01/q001?value=known").text
    assert "banner" not in body and "examkb" not in body


# --------------------------------------------------------------------------- the CLI


def examkb(*arguments: str, database: Path) -> subprocess.CompletedProcess:
    import os

    return subprocess.run(
        [sys.executable, "-m", "examkb.cli", *arguments],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": f"sqlite:///{database}"},
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_command_imports_and_is_idempotent(real_db: Engine, tmp_path: Path) -> None:
    database = Path(real_db.url.database)
    real_db.dispose()  # checkpoint the WAL so the subprocess sees the projection
    blob = tmp_path / "kb-marks.json"
    blob.write_text(
        json.dumps(
            {
                "kb-theme": "dark",
                "kb-marks": json.dumps(
                    {"ccao-f/exam-01/q001": "known", "gone/exam-99/q001": "unsure"}
                ),
            }
        ),
        encoding="utf-8",
    )

    first = examkb("import-marks", str(blob), database=database)
    second = examkb("import-marks", str(blob), database=database)

    assert first.returncode == 0, first.stderr
    assert "1 imported" in first.stdout
    assert "not in the corpus" in first.stdout
    assert second.returncode == 0
    assert "0 imported, 1 already current" in second.stdout


def test_a_dry_run_writes_nothing(real_db: Engine, tmp_path: Path) -> None:
    database = Path(real_db.url.database)
    real_db.dispose()
    blob = tmp_path / "kb-marks.json"
    blob.write_text(json.dumps({"ccao-f/exam-01/q001": "known"}), encoding="utf-8")

    result = examkb("import-marks", str(blob), "--dry-run", database=database)

    assert result.returncode == 0
    assert "nothing was written" in result.stdout
    import sqlite3

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT count(*) FROM mark").fetchone()[0] == 0
    finally:
        connection.close()


def test_a_missing_file_is_a_sentence_not_a_traceback(real_db: Engine, tmp_path: Path) -> None:
    database = Path(real_db.url.database)
    real_db.dispose()

    result = examkb("import-marks", str(tmp_path / "nope.json"), database=database)

    assert result.returncode == 1
    assert "no such file" in result.stderr
    assert "Traceback" not in result.stderr


# ------------------------------------------------------- every filter reaches the route


def test_every_filter_parameter_is_accepted_by_the_route(marked: str) -> None:
    """The gap that let `?mark=` be silently ignored: the route's own signature.

    `FILTER_PARAMS` is the map, the facets read it, the templates build links from
    it -- and none of that makes FastAPI accept the query parameter. Each one is
    driven over HTTP here against a value that genuinely narrows the corpus, so a
    parameter the route drops shows up as "still 549".
    """
    client = TestClient(
        create_app(
            database_url=marked,
            status_provider=lambda: status_module.projection_status(url=marked),
        )
    )
    narrowing = {
        "cert": "ccao-f",
        "exam": "ccao-f/exam-01",
        "mode": "realistic",
        "domain": "Output Evaluation and Validation",
        "type": "multi_select",
        "mark": "known",
    }
    assert set(narrowing) == set(browse_service_filter_params())

    for param, value in narrowing.items():
        expected = browse_page(url=marked, params={param: value}).total
        assert 0 < expected < 549, f"{param}={value} does not narrow anything"

        body = client.get("/browse", params={param: value}).text
        shown = int(body.split("<strong>")[1].split("</strong>")[0])
        assert shown == expected, f"?{param}= was ignored by the route"


def browse_service_filter_params() -> dict[str, str]:
    from examkb.services import browse as service

    return service.FILTER_PARAMS
