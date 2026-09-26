"""The results page, and the end of V1a.

This is the last surface in the LLM-free slice, so one of the tests here is the
slice's own gate: sit an exam, submit it, grade it, read it, and assert that
`llm_call` is still empty. That claim has been made in the plan since phase 0 and
nothing has ever checked it.

The rest is about the page being a record rather than a view. It is built from
`attempt_item.snapshot_json` and the stored permutation, so a rebuild of the
corpus underneath it changes nothing -- asserted by rebuilding the corpus. And it
is the only page that renders the answer key, which is why an **open** attempt has
no results page at all: otherwise the URL is a second tab that reads the key
mid-exam, and 015's guarantee is worth nothing.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import write_kb
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from examkb import db as db_module
from examkb import status as status_module
from examkb.ingest import ingest
from examkb.models import journal
from examkb.services import results as results_service
from examkb.services import runner as runner_service
from examkb.services.attempts import abandon, answer, start, submit, view
from examkb.web.app import create_app

PERCENT = re.compile(r"\d+%")


@pytest.fixture(autouse=True)
def clean_caches():
    db_module.engine_for.cache_clear()
    status_module.forget_corpus_fingerprint()
    yield
    db_module.engine_for.cache_clear()
    status_module.forget_corpus_fingerprint()


@pytest.fixture
def url(real_db: Engine) -> str:
    return str(real_db.url)


@pytest.fixture
def client(url: str) -> TestClient:
    app = create_app(
        database_url=url, status_provider=lambda: status_module.projection_status(url=url)
    )
    return TestClient(app, follow_redirects=False)


def sit(url: str, *, count: int = 8, seed: int = 5, answers: str = "correct") -> int:
    """Sit and submit an exam. `answers` is correct / wrong / blank / mixed."""
    attempt_id = runner_service.begin(certification_id="ccao-f", count=count, seed=seed, url=url)
    with Session(db_module.engine_for(url)) as session:
        items = session.scalars(
            sa.select(journal.AttemptItem)
            .where(journal.AttemptItem.attempt_id == attempt_id)
            .order_by(journal.AttemptItem.position)
        ).all()
        for item in items:
            snapshot = item.snapshot_json
            correct = snapshot["correct_labels"]
            if answers == "blank" or (answers == "mixed" and item.position % 3 == 0):
                continue
            if answers == "correct" or (answers == "mixed" and item.position % 3 == 1):
                picked = correct
            else:
                picked = [
                    option["label"]
                    for option in snapshot["options"]
                    if option["label"] not in correct
                ][: snapshot["select_count"]]
            answer(session, attempt_id, item.position, picked)
        submit(session, attempt_id)
        session.commit()
    return attempt_id


# --------------------------------------------------------------------- the replay


def test_the_replay_is_in_the_order_sat_with_the_order_shown(url: str) -> None:
    """The criterion. Both orders come from the stored rows, not from the corpus."""
    attempt_id = sit(url, count=8, seed=5, answers="mixed")

    with Session(db_module.engine_for(url)) as session:
        stored = [
            (item.position, item.question_id, list(item.option_order))
            for item in session.scalars(
                sa.select(journal.AttemptItem)
                .where(journal.AttemptItem.attempt_id == attempt_id)
                .order_by(journal.AttemptItem.position)
            )
        ]

    page = results_service.page(attempt_id, url=url)

    assert [item.position for item in page.items] == [row[0] for row in stored]
    assert [item.question_id for item in page.items] == [row[1] for row in stored]
    for item, (_position, _question_id, order) in zip(page.items, stored):
        assert [option.label for option in item.options] == order


def test_the_replay_marks_what_was_picked_and_what_was_right(url: str) -> None:
    attempt_id = sit(url, count=6, seed=9, answers="wrong")
    page = results_service.page(attempt_id, url=url)

    for item in page.items:
        picked = {option.label for option in item.options if option.selected}
        right = {option.label for option in item.options if option.correct}
        assert picked == set(item.selected)
        assert right == set(item.correct_labels)
        assert not item.is_correct
        assert {option.state for option in item.options} <= {"right", "missed", "wrong", "plain"}
        assert "missed" in {option.state for option in item.options}


def test_the_page_carries_the_key_and_every_explanation(url: str) -> None:
    """Unlike the runner. This panel is what 041 builds its chat context from."""
    attempt_id = sit(url, count=4, seed=3)
    page = results_service.page(attempt_id, url=url)

    for item in page.items:
        assert item.correct_labels
        assert any(option.explanation_md for option in item.options)
    assert any(item.overall_explanation_md for item in page.items)


def test_the_snapshot_wins_over_a_rebuilt_corpus(
    tmp_session: Session, tmp_kb: Path, mini_questions: list[dict]
) -> None:
    """The criterion, done by rebuilding the corpus rather than asserting nothing writes."""
    ingest(tmp_session, tmp_kb)
    tmp_session.commit()
    attempt = start(tmp_session, certification_id="mini-a", count=2, seed=1)
    for item in tmp_session.scalars(sa.select(journal.AttemptItem)):
        answer(tmp_session, attempt.id, item.position, item.snapshot_json["correct_labels"])
    submit(tmp_session, attempt.id)
    tmp_session.commit()
    url = str(tmp_session.get_bind().url)
    before = results_service.page(attempt.id, url=url)

    moved = json.loads(json.dumps(mini_questions))
    for question in moved:
        question["prompt_md"] = "REWRITTEN"
        for option in question["options"]:
            option["text_md"] = "REWRITTEN OPTION"
    write_kb(tmp_kb, moved)
    ingest(tmp_session, tmp_kb, rebuild=True)
    tmp_session.commit()
    db_module.engine_for.cache_clear()

    after = results_service.page(attempt.id, url=url)

    assert [item.prompt_md for item in after.items] == [item.prompt_md for item in before.items]
    assert "REWRITTEN" not in " ".join(item.prompt_md for item in after.items)
    assert tmp_session.scalar(sa.text("SELECT prompt_md FROM question LIMIT 1")) == "REWRITTEN"


# ------------------------------------------------------------------- the domains


def test_every_percentage_is_beside_its_count(client: TestClient, url: str) -> None:
    """The criterion: a domain with two questions never says "50%" on its own."""
    attempt_id = sit(url, count=12, seed=7, answers="mixed")
    body = client.get(f"/results/{attempt_id}").text

    rows = re.findall(r"<li>\s*<span class=\"d\">.*?</li>", body, re.S)
    assert rows, "no domain rows rendered"
    for row in rows:
        assert PERCENT.search(row), row[:80]
        assert re.search(r"\d+ of \d+", row), f"a percentage with no n: {row[:120]}"


def test_a_two_question_domain_says_one_of_two(url: str) -> None:
    """The criterion's own example, built deliberately rather than hoped for."""
    page_items = results_service.DomainRow(label="Tiny", correct=1, asked=2)

    assert page_items.share == "1 of 2"
    assert page_items.percent == 50.0


def test_domains_are_worst_first(url: str) -> None:
    """The page is for finding what to study."""
    attempt_id = sit(url, count=20, seed=11, answers="mixed")
    page = results_service.page(attempt_id, url=url)

    shares = [row.correct / row.asked for row in page.domains]
    assert shares == sorted(shares)


def test_the_domain_counts_add_up_to_the_exam(url: str) -> None:
    attempt_id = sit(url, count=15, seed=13, answers="mixed")
    page = results_service.page(attempt_id, url=url)

    assert sum(row.asked for row in page.domains) == page.grade.item_count == 15
    assert sum(row.correct for row in page.domains) == page.grade.correct_count


# -------------------------------------------------------------------- the charts


def test_the_chart_is_inline_svg_and_fetches_nothing(client: TestClient, url: str) -> None:
    """The criterion. Anchors in the explanations are content; resources are not."""
    attempt_id = sit(url, count=10, seed=5, answers="mixed")
    body = client.get(f"/results/{attempt_id}").text

    assert body.count('<svg class="chart"') == 1
    resources = re.findall(
        r'(?:<img[^>]+src|<script[^>]+src|<link[^>]+href)="([^"]+)"', body
    )
    assert resources, "the page loads something"
    for resource in resources:
        assert resource.startswith("/static/") or resource.startswith("data:"), resource


def test_the_chart_uses_the_palettes_css_variables(client: TestClient, url: str) -> None:
    """One chart body serves light and dark because the colours are tokens."""
    attempt_id = sit(url, count=10, seed=5)
    chart = client.get(f"/results/{attempt_id}").text.split('<svg class="chart"')[1].split("</svg>")[0]

    assert "var(--series-1)" in chart
    assert "var(--text-secondary)" in chart
    assert "#" not in chart, "a literal colour would not follow the theme"


def test_charts_py_is_reused_not_copied() -> None:
    """The plan's "zero porting cost" claim, asserted rather than repeated.

    `examkb/web/charts.py` exists, but it is a wrapper: it must draw nothing of its
    own, or the reports and the app would be two chart libraries that agree until
    somebody fixes a bug in one of them.
    """
    import inspect

    from examkb.compat import charts

    from examkb.web import charts as wrapper

    root = Path(__file__).resolve().parent.parent
    assert Path(charts.__file__).resolve() == root / "tools" / "charts.py"

    source = inspect.getsource(wrapper)
    assert "<svg" not in source and "<rect" not in source and "<path" not in source
    assert "_rounded_bar" not in source
    for name in ("hbar", "table", "stat_tile"):
        assert f"charts.{name}(" in source, f"{name} does not delegate"


def test_the_reports_and_the_app_draw_the_same_bar() -> None:
    """Same function, same arguments, same bytes -- which is the whole claim."""
    from examkb.compat import charts

    from examkb.web import charts as wrapper

    items = [("Integration", 50.0), ("Evaluation", 75.0)]
    assert str(wrapper.hbar(items, label="x")) == charts.hbar(items, label="x")


def test_the_chart_ships_with_a_table_view(client: TestClient, url: str) -> None:
    attempt_id = sit(url, count=10, seed=5, answers="mixed")
    body = client.get(f"/results/{attempt_id}").text

    assert 'class="table-view"' in body
    assert "<th>Domain</th>" in body and "<th>Asked</th>" in body


# ----------------------------------------------------------------- the edge cases


def test_an_exam_with_no_answers_renders(client: TestClient, url: str) -> None:
    """The criterion."""
    attempt_id = sit(url, count=6, seed=17, answers="blank")

    response = client.get(f"/results/{attempt_id}")
    page = results_service.page(attempt_id, url=url)

    assert response.status_code == 200
    assert page.grade.correct_count == 0
    assert page.grade.answered_count == 0
    assert page.blank_count == 6
    assert page.grade.credit_total == 0.0, "blank costs nothing"
    assert "6 left blank" in response.text


def test_an_exam_answered_entirely_wrongly_renders(client: TestClient, url: str) -> None:
    attempt_id = sit(url, count=6, seed=19, answers="wrong")

    response = client.get(f"/results/{attempt_id}")
    page = results_service.page(attempt_id, url=url)

    assert response.status_code == 200
    assert page.grade.correct_count == 0
    assert page.grade.credit_total < 0
    assert all(row.percent == 0.0 for row in page.domains)


def test_a_perfect_exam_passes(client: TestClient, url: str) -> None:
    attempt_id = sit(url, count=6, seed=21, answers="correct")
    page = results_service.page(attempt_id, url=url)

    assert page.grade.correct_count == 6
    assert page.grade.scaled_score == 1000 and page.grade.passed
    assert "pass" in client.get(f"/results/{attempt_id}").text


# ------------------------------------------------------- the key is not a side door


def test_an_open_attempt_has_no_results_page(client: TestClient, url: str) -> None:
    """The criterion. Otherwise the URL is a second tab that reads the key mid-exam."""
    attempt_id = runner_service.begin(certification_id="ccao-f", count=5, seed=23, url=url)

    response = client.get(f"/results/{attempt_id}")

    assert response.status_code == 409
    assert "still open" in response.text
    assert "correct_labels" not in response.text


def test_the_service_refuses_an_open_attempt_too(url: str) -> None:
    attempt_id = runner_service.begin(certification_id="ccao-f", count=5, seed=25, url=url)

    with pytest.raises(results_service.ResultsNotReady, match="still open"):
        results_service.page(attempt_id, url=url)


def test_an_abandoned_attempt_is_not_scored(client: TestClient, url: str) -> None:
    attempt_id = runner_service.begin(certification_id="ccao-f", count=5, seed=27, url=url)
    with Session(db_module.engine_for(url)) as session:
        abandon(session, attempt_id)
        session.commit()

    response = client.get(f"/results/{attempt_id}")

    assert response.status_code == 409
    assert "abandoned" in response.text


def test_an_unknown_attempt_is_a_404(client: TestClient) -> None:
    assert client.get("/results/999").status_code == 404


# -------------------------------------------------------------------- the grading


def test_the_page_grades_on_first_view_and_not_again(client: TestClient, url: str) -> None:
    attempt_id = sit(url, count=5, seed=29, answers="mixed")
    with Session(db_module.engine_for(url)) as session:
        assert session.get(journal.Attempt, attempt_id).graded_at is None

    first = results_service.page(attempt_id, url=url)
    stamped = first.grade.graded_at
    second = results_service.page(attempt_id, url=url)

    assert not first.grade.already_graded
    assert second.grade.already_graded
    assert second.grade.graded_at == stamped
    assert client.get(f"/results/{attempt_id}").status_code == 200


def test_the_page_says_the_scaled_score_is_a_model(client: TestClient, url: str) -> None:
    """The endpoints and the cut are published; the line between them is ours."""
    attempt_id = sit(url, count=5, seed=31)
    body = client.get(f"/results/{attempt_id}").text

    assert "linear map" in body
    assert "not the vendor's equating" in body
    assert "720" in body


def test_submitting_lands_here(client: TestClient, url: str) -> None:
    attempt_id = runner_service.begin(certification_id="ccao-f", count=4, seed=33, url=url)

    response = client.post(f"/exam/{attempt_id}/submit")

    assert response.status_code == 303
    assert response.headers["location"] == f"/results/{attempt_id}"


def test_the_runner_redirects_to_results_once_closed(client: TestClient, url: str) -> None:
    attempt_id = sit(url, count=4, seed=35)

    response = client.get(f"/exam/{attempt_id}/q/0")

    assert response.status_code == 303
    assert response.headers["location"] == f"/results/{attempt_id}"


def test_the_index_lists_finished_attempts(client: TestClient, url: str) -> None:
    first = sit(url, count=4, seed=37, answers="correct")
    second = sit(url, count=4, seed=39, answers="wrong")

    body = client.get("/results").text

    assert f"/results/{first}" in body and f"/results/{second}" in body
    assert body.index(f"/results/{second}") < body.index(f"/results/{first}"), "newest first"


def test_the_nav_now_links_to_results(client: TestClient) -> None:
    body = client.get("/").text

    assert 'href="/results"' in body
    assert 'data-issue="016"' not in body


# ------------------------------------------------------------------- the V1a gate


def test_no_llm_call_is_ever_made(client: TestClient, url: str) -> None:
    """**The V1a gate.** Sit, submit, grade, read -- and the call log is empty.

    The plan has claimed since phase 0 that the whole LLM-free slice makes zero
    model calls. 016 is the last thing in it, so this is where the claim gets
    checked instead of repeated. The first call in this project is 029's.
    """
    attempt_id = sit(url, count=10, seed=41, answers="mixed")

    assert client.get(f"/results/{attempt_id}").status_code == 200
    assert client.get("/results").status_code == 200
    assert client.get("/browse?q=caching").status_code == 200

    with Session(db_module.engine_for(url)) as session:
        assert session.scalar(sa.text("SELECT count(*) FROM llm_call")) == 0
        assert session.scalar(sa.text("SELECT count(*) FROM job")) == 0
        assert session.scalar(sa.text("SELECT count(*) FROM candidate")) == 0


def test_the_whole_loop_works_end_to_end(client: TestClient, url: str) -> None:
    """Sit an exam through the HTTP surface, submit it, and read the score back."""
    started = client.post("/exam/start", data={"certification_id": "ccao-f", "count": "6"})
    attempt_id = int(started.headers["location"].rsplit("/", 1)[-1])

    correct = 0
    for position in range(6):
        item = runner_service.page(attempt_id, position, url=url).item
        with Session(db_module.engine_for(url)) as session:
            key = session.scalars(
                sa.select(journal.AttemptItem.snapshot_json).where(
                    journal.AttemptItem.attempt_id == attempt_id,
                    journal.AttemptItem.position == position,
                )
            ).one()["correct_labels"]
        picked = key if position % 2 == 0 else [item.options[0].label]
        assert client.post(
            f"/exam/{attempt_id}/answer", data={"position": position, "label": picked}
        ).status_code == 204
        correct += int(sorted(picked) == sorted(key))

    assert client.post(f"/exam/{attempt_id}/submit").status_code == 303
    body = client.get(f"/results/{attempt_id}").text

    assert f"{correct} of 6" in body
    assert body.count('class="replay-item') == 6
