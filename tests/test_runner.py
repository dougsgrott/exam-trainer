"""The exam runner's server half.

Integration fix 3 is the reason this file exists, and it is a claim about a
*response*: nothing the server sends back on an answer can replace the control
the person is clicking, because nothing comes back at all. `204`, no body, an
`HX-Trigger` header. That is testable without a browser and it is tested here
exhaustively -- every write endpoint, every time.

The browser half -- that two clicks 200 ms apart both reach the server -- is
`tests/test_runner_e2e.py`, which needs Chromium and skips with the command to
install it until one is available.

The other thing asserted here is what the runner does *not* send: the snapshot it
renders from holds the answer key, and no page may carry it. That is checked
against every question of a real attempt rather than against one.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from examkb import db as db_module
from examkb import status as status_module
from examkb.models import journal
from examkb.models.base import utcnow
from examkb.services import runner as runner_service
from examkb.services.attempts import AttemptError
from examkb.web.app import create_app

TRIGGER = "examkb:saved"


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
    return str(real_db.url)


@pytest.fixture
def client(url: str) -> TestClient:
    app = create_app(
        database_url=url, status_provider=lambda: status_module.projection_status(url=url)
    )
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def sitting(url: str) -> int:
    """A five-question ccao-f attempt, open."""
    return runner_service.begin(certification_id="ccao-f", count=5, seed=7, url=url)


def multi_position(url: str, attempt_id: int) -> int | None:
    """The position of a 2-of-4 in this attempt, if it drew one."""
    page = runner_service.page(attempt_id, 0, url=url)
    for entry in page.map:
        item = runner_service.page(attempt_id, entry.position, url=url).item
        if item.multi:
            return entry.position
    return None


def trigger_of(response) -> dict:
    return json.loads(response.headers["HX-Trigger"])[TRIGGER]


# ------------------------------------------------------- integration fix 3, asserted


@pytest.mark.parametrize("endpoint", ["answer", "flag"])
def test_every_write_returns_204_with_no_body(client: TestClient, sitting: int, endpoint) -> None:
    """The criterion. An empty body is the guarantee, not an optimisation.

    There is nothing for HTMX to swap in, so there is no way for a response to
    detach the checkbox the second click is about to land on.
    """
    data = {"position": 0}
    if endpoint == "answer":
        data["label"] = ["A"]

    response = client.post(f"/exam/{sitting}/{endpoint}", data=data)

    assert response.status_code == 204
    assert response.content == b""
    assert "HX-Trigger" in response.headers
    assert "content-type" not in {k.lower() for k in response.headers}


def test_the_answer_form_never_asks_for_a_swap(client: TestClient, sitting: int) -> None:
    body = client.get(f"/exam/{sitting}/q/0").text

    assert body.count('hx-swap="none"') >= 2, "the answer form and the flag form"
    assert "outerHTML" not in body
    assert "innerHTML" not in body
    assert 'hx-target' not in body, "nothing is targeted because nothing is swapped"


def test_the_trigger_carries_what_the_page_needs_to_resync(
    client: TestClient, sitting: int
) -> None:
    response = client.post(f"/exam/{sitting}/answer", data={"position": 1, "label": ["B"]})
    detail = trigger_of(response)

    assert detail["position"] == 1
    assert detail["selected"] == ["B"]
    assert detail["answered"] == 1 and detail["count"] == 5
    assert isinstance(detail["remaining"], int)


def test_the_script_only_touches_things_with_no_inputs_in_them() -> None:
    """The client half of the same promise, read off the file.

    `runner.js` may update the counter, the map, the clock and the save state.
    If it ever starts replacing the question, this is the test that says so.
    """
    source = (Path("examkb/web/static/runner.js")).read_text(encoding="utf-8")

    for forbidden in ("innerHTML", "outerHTML", "replaceWith", "removeChild"):
        assert forbidden not in source, f"runner.js uses {forbidden}"
    assert "textContent" in source


# --------------------------------------------------------------- the position travels


def test_the_position_comes_from_the_form_not_from_a_cursor(
    client: TestClient, sitting: int, url: str
) -> None:
    """The criterion: a save in flight during a navigation still lands correctly.

    There is no server-side "current question", so the two cannot race. This is
    the out-of-order case written down: answer 3 is sent while the browser is
    already showing question 0, and it still belongs to question 3.
    """
    client.get(f"/exam/{sitting}/q/3")
    client.get(f"/exam/{sitting}/q/0")
    client.post(f"/exam/{sitting}/answer", data={"position": 3, "label": ["C"]})

    page = runner_service.page(sitting, 3, url=url)
    assert [option.label for option in page.item.options if option.selected] == ["C"]
    assert not any(option.selected for option in runner_service.page(sitting, 0, url=url).item.options)


def test_rapid_navigation_never_misattributes_an_answer(
    client: TestClient, sitting: int, url: str
) -> None:
    """next / next / back, with a save fired from each page after leaving it."""
    for position in (0, 1, 2, 1, 0):
        client.get(f"/exam/{sitting}/q/{position}")
    for position, label in ((0, "A"), (1, "B"), (2, "D")):
        client.post(f"/exam/{sitting}/answer", data={"position": position, "label": [label]})

    for position, label in ((0, "A"), (1, "B"), (2, "D")):
        page = runner_service.page(sitting, position, url=url)
        assert [o.label for o in page.item.options if o.selected] == [label]


def test_a_position_outside_the_exam_is_clamped_not_a_crash(
    client: TestClient, sitting: int
) -> None:
    assert client.get(f"/exam/{sitting}/q/999").status_code == 200
    assert client.get(f"/exam/{sitting}/q/0").status_code == 200


# --------------------------------------------------------------------- what it shows


def test_the_runner_never_shows_the_answer_key(client: TestClient, sitting: int, url: str) -> None:
    """The criterion. The snapshot holds a key; no page may carry it.

    Checked against every question of the attempt and against the *actual* correct
    labels, not against the word "correct" -- an option's text could contain it.
    """
    with Session(db_module.engine_for(url)) as session:
        keys = {
            item.position: (item.snapshot_json["correct_labels"], item.snapshot_json["options"])
            for item in session.scalars(
                sa.select(journal.AttemptItem).where(journal.AttemptItem.attempt_id == sitting)
            )
        }

    for position, (correct, options) in keys.items():
        body = client.get(f"/exam/{sitting}/q/{position}").text
        assert "correct_labels" not in body
        assert "is_correct" not in body
        for option in options:
            assert (option.get("explanation_md") or "\x00") not in body
        # The letters themselves are on the page; what must not be is any marking.
        assert not re.search(r'data-correct|class="[^"]*\bcorrect\b', body), position
    assert any(labels for labels, _ in keys.values()), "there were keys to leak"


def test_no_route_in_the_runner_offers_chat(client: TestClient, sitting: int) -> None:
    """The criterion, asked of the links rather than of the prose.

    The corpus is about Claude, so the word "chat" is all over the questions. What
    matters is that nothing on the page *points* at a chat route -- 041's gate
    refuses a thread while an attempt is open, and a button that always errors is
    worse than no button.
    """
    for path in (f"/exam/{sitting}/q/0", "/exam"):
        body = client.get(path, follow_redirects=True).text
        targets = re.findall(r'(?:href|action|hx-post|hx-get)="([^"]+)"', body)
        assert not [target for target in targets if "chat" in target.lower()]


def test_the_page_shows_the_progress_and_the_map(client: TestClient, sitting: int) -> None:
    body = client.get(f"/exam/{sitting}/q/2").text

    assert "Question 3 of 5" in body
    assert body.count('class="qmap"') == 1
    qmap = body.split('class="qmap"')[1].split("</ol>")[0]
    assert qmap.count("data-position=") == 5
    assert 'aria-current="true"' in body


def test_a_multi_select_renders_checkboxes_and_says_how_many(
    client: TestClient, sitting: int, url: str
) -> None:
    position = multi_position(url, sitting)
    if position is None:
        pytest.skip("this seed drew no multi-select")

    body = client.get(f"/exam/{sitting}/q/{position}").text

    assert 'type="checkbox"' in body
    assert "select 2" in body


def test_a_single_select_renders_radios(client: TestClient, sitting: int, url: str) -> None:
    single = next(
        entry.position
        for entry in runner_service.page(sitting, 0, url=url).map
        if not runner_service.page(sitting, entry.position, url=url).item.multi
    )

    assert 'type="radio"' in client.get(f"/exam/{sitting}/q/{single}").text


# ------------------------------------------------------------------- resuming


def test_reloading_restores_every_selection_and_the_same_order(
    client: TestClient, sitting: int, url: str
) -> None:
    """The criterion."""
    before = {}
    for position in range(5):
        page = runner_service.page(sitting, position, url=url)
        before[position] = [option.label for option in page.item.options]
        client.post(
            f"/exam/{sitting}/answer", data={"position": position, "label": [before[position][1]]}
        )

    for position in range(5):
        body = client.get(f"/exam/{sitting}/q/{position}").text
        shown = re.findall(r'name="label" value="([A-Z])"', body)
        checked = re.findall(r'name="label" value="([A-Z])"\s*\n?\s*checked', body)

        assert shown == before[position], "the option order moved on reload"
        assert checked or 'checked' in body


def test_opening_the_attempt_lands_on_the_first_unanswered(
    client: TestClient, sitting: int
) -> None:
    client.post(f"/exam/{sitting}/answer", data={"position": 0, "label": ["A"]})
    client.post(f"/exam/{sitting}/answer", data={"position": 1, "label": ["A"]})

    response = client.get(f"/exam/{sitting}")

    assert response.status_code == 303
    assert response.headers["location"] == f"/exam/{sitting}/q/2"


# ---------------------------------------------------------------------- the clock


def test_the_clock_comes_from_the_server(url: str, sitting: int) -> None:
    """The criterion: the timer cannot be extended by the client's clock.

    `remaining_seconds` is computed from `attempt.started_at` and the recorded
    limit, on the server, every time anything is asked. The page is told a number;
    it is never asked for one.
    """
    started = runner_service.page(sitting, 0, url=url)
    assert started.remaining_seconds == 120 * 60

    with Session(db_module.engine_for(url)) as session:
        attempt = session.get(journal.Attempt, sitting)
        later = attempt.started_at + timedelta(minutes=30)

    assert runner_service.page(sitting, 0, url=url, now=later).remaining_seconds == 90 * 60


def test_the_clock_floors_at_zero_rather_than_going_negative(url: str, sitting: int) -> None:
    with Session(db_module.engine_for(url)) as session:
        started = session.get(journal.Attempt, sitting).started_at

    page = runner_service.page(sitting, 0, url=url, now=started + timedelta(hours=5))

    assert page.remaining_seconds == 0
    assert page.out_of_time
    assert page.remaining_label == "0:00:00"


def test_every_save_resyncs_the_clock(client: TestClient, sitting: int) -> None:
    response = client.post(f"/exam/{sitting}/answer", data={"position": 0, "label": ["A"]})

    assert trigger_of(response)["remaining"] <= 120 * 60


def test_the_script_never_reads_the_wall_clock_for_the_deadline() -> None:
    """`Date.now()` appears, for time-on-question; the deadline is the server's."""
    source = Path("examkb/web/static/runner.js").read_text(encoding="utf-8")

    assert "the server's clock wins" in source
    assert "remaining = detail.remaining" in source


# -------------------------------------------------------------------- flag for review


def test_flagging_uses_the_same_mark_browse_filters_on(
    client: TestClient, sitting: int, url: str
) -> None:
    """One fact, one place: the post-exam review list is `/browse?mark=flagged`."""
    question_id = runner_service.page(sitting, 0, url=url).item.question_id

    assert trigger_of(client.post(f"/exam/{sitting}/flag", data={"position": 0}))["flagged"]

    with Session(db_module.engine_for(url)) as session:
        assert session.scalar(
            sa.text("SELECT value FROM current_mark WHERE question_id = :id"), {"id": question_id}
        ) == "flagged"

    assert runner_service.page(sitting, 0, url=url).item.flagged
    assert not trigger_of(client.post(f"/exam/{sitting}/flag", data={"position": 0}))["flagged"]


def test_a_flag_shows_in_the_question_map(client: TestClient, sitting: int) -> None:
    client.post(f"/exam/{sitting}/flag", data={"position": 2})

    body = client.get(f"/exam/{sitting}/q/0").text
    assert re.search(r'class="open flagged"[^>]*data-position="2"', body)


# -------------------------------------------------------------------- start and finish


def test_the_start_page_offers_the_certifications(client: TestClient) -> None:
    body = client.get("/exam").text

    assert "ccao-f" in body and "ccar-p" in body
    assert "360 questions" in body and "189 questions" in body


def test_an_open_attempt_is_offered_for_resume_rather_than_a_second_start(
    client: TestClient, sitting: int
) -> None:
    body = client.get("/exam").text

    assert "exam in progress" in body
    assert f"/exam/{sitting}" in body
    assert "<form method=\"post\" action=\"/exam/start\"" not in body


def test_starting_a_second_attempt_is_a_conflict(client: TestClient, sitting: int) -> None:
    response = client.post("/exam/start", data={"certification_id": "ccao-f", "count": "5"})

    assert response.status_code == 409
    # A plain form post, so the browser gets the error *page*, not JSON.
    assert "still open" in response.text
    assert f"attempt {sitting}" in response.text


def test_submitting_closes_the_attempt(client: TestClient, sitting: int, url: str) -> None:
    client.post(f"/exam/{sitting}/answer", data={"position": 0, "label": ["A"]})

    response = client.post(f"/exam/{sitting}/submit")

    assert response.status_code == 303
    # 016 took this redirect's target; 015 shipped a placeholder and said it would.
    assert response.headers["location"] == f"/results/{sitting}"
    assert not runner_service.page(sitting, 0, url=url).open


def test_a_closed_attempt_redirects_away_from_the_runner(
    client: TestClient, sitting: int
) -> None:
    client.post(f"/exam/{sitting}/submit")

    response = client.get(f"/exam/{sitting}/q/0")

    assert response.status_code == 303
    assert response.headers["location"] == f"/results/{sitting}"


def test_answering_after_submit_is_a_conflict(client: TestClient, sitting: int) -> None:
    client.post(f"/exam/{sitting}/submit")

    response = client.post(f"/exam/{sitting}/answer", data={"position": 0, "label": ["A"]})

    assert response.status_code == 409


def test_abandoning_returns_to_the_start_page(client: TestClient, sitting: int) -> None:
    response = client.post(f"/exam/{sitting}/abandon")

    assert response.status_code == 303
    assert response.headers["location"] == "/exam"
    assert "exam in progress" not in client.get("/exam").text


def test_the_submit_button_names_the_unanswered_count(client: TestClient, sitting: int) -> None:
    body = client.get(f"/exam/{sitting}/q/0").text
    assert 'id="unanswered">5<' in body

    client.post(f"/exam/{sitting}/answer", data={"position": 0, "label": ["A"]})
    assert 'id="unanswered">4<' in client.get(f"/exam/{sitting}/q/0").text


def test_an_unknown_attempt_is_a_404(client: TestClient) -> None:
    assert client.get("/exam/999/q/0").status_code == 404


# ------------------------------------------------------------------------- the nav


def test_the_nav_now_links_to_exams(client: TestClient) -> None:
    body = client.get("/").text

    assert 'href="/exam"' in body
    assert 'data-issue="015"' not in body


# ------------------------------------------------------------------- the service


def test_the_service_refuses_a_flag_outside_the_exam(url: str, sitting: int) -> None:
    with pytest.raises(AttemptError, match="no item at position"):
        runner_service.toggle_flag(sitting, 99, url=url)


def test_the_runner_item_carries_no_key(url: str, sitting: int) -> None:
    """Structural: the dataclass has no field the key could travel in."""
    item = runner_service.page(sitting, 0, url=url).item

    assert not hasattr(item, "correct_labels")
    assert not any("correct" in name for name in vars(item))
    assert not any(hasattr(option, "is_correct") for option in item.options)
