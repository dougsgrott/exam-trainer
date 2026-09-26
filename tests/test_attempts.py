"""The attempt lifecycle: the one record here that cannot be rebuilt.

`kb/` regenerates from `data/`, the projection regenerates from `kb/`, a mark can
be re-marked. An attempt is a thing that happened once, so the tests that matter
are the ones about it not changing afterwards:

- the snapshot survives the corpus being rebuilt underneath it, asserted by
  actually rebuilding the corpus underneath it;
- the option order a resumed exam shows is the one that was stored, not a fresh
  shuffle;
- an abandoned attempt reaches no statistic;
- and `snapshot_json` refuses to be updated, proved against SQLite rather than
  against the service, because the service is not the only thing that can hold a
  connection to this file.

The last one is why "open" needed a migration. 005 gave the chat gate
`submitted_at IS NULL` and called it the single definition; abandoning leaves that
NULL, so an abandoned exam used to block chat about its questions for ever. 0003
makes the trigger and the service say the same thing, and a test here reads both.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import write_kb
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from examkb import db as db_module
from examkb.ingest import ingest
from examkb.models import journal
from examkb.services import attempts
from examkb.services.attempts import AttemptError, abandon, answer, open_attempt, start, submit, view
from examkb.services.search import SearchFilters

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
Q1 = "mini-a/exam-01/q001"
Q2 = "mini-a/exam-01/q002"


@pytest.fixture(autouse=True)
def clean_caches():
    db_module.engine_for.cache_clear()
    db_module.forget_projection_ready()
    yield
    db_module.engine_for.cache_clear()
    db_module.forget_projection_ready()


@pytest.fixture
def mini(tmp_session: Session, tmp_kb: Path) -> Session:
    """The two-question mini-a corpus, projected."""
    ingest(tmp_session, tmp_kb)
    tmp_session.commit()
    return tmp_session


@pytest.fixture
def sitting(mini: Session) -> journal.Attempt:
    """An open two-item attempt on mini-a."""
    attempt = start(mini, certification_id="mini-a", count=2, seed=11, now=NOW)
    mini.commit()
    return attempt


def labels_shown(session: Session, attempt_id: int) -> list[list[str]]:
    return [[option.label for option in item.options] for item in view(session, attempt_id).items]


# -------------------------------------------------------------------------- starting


def test_starting_freezes_the_whole_exam(sitting, mini: Session) -> None:
    got = view(mini, sitting.id)

    assert got.count == 2 == sitting.item_count
    assert got.open and got.answered_count == 0
    assert {item.question_id for item in got.items} == {Q1, Q2}
    for item in got.items:
        assert item.prompt_md
        assert len(item.options) == 4
        assert item.seen_ordinal == 1


def test_the_attempt_records_the_draw_it_came_from(sitting) -> None:
    """013 stores what 012 returned, so a sitting explains its own question set."""
    assert sitting.seed == 11
    assert sitting.requested_count == 2
    assert sitting.sampler_json["seed"] == 11
    assert sitting.sampler_json["drawn"] == 2
    assert sitting.time_limit_seconds == 120 * 60


def test_the_snapshot_holds_the_key_even_though_the_runner_hides_it(sitting, mini: Session) -> None:
    """A snapshot is the question's state, not the pixels.

    016 replays a sitting after 040 has overridden the key, and has to be able to
    say what was correct *then*. That is only possible if the key was written down.
    """
    item = mini.scalars(
        sa.select(journal.AttemptItem).where(journal.AttemptItem.question_id == Q1)
    ).one()

    assert item.snapshot_json["correct_labels"] == ["A"]
    assert all(option["explanation_md"] for option in item.snapshot_json["options"])
    # And the view a page gets does not carry it.
    shown = attempts.item_view(item)
    assert not hasattr(shown.options[0], "is_correct")


def test_only_one_attempt_may_be_open_at_a_time(sitting, mini: Session) -> None:
    with pytest.raises(AttemptError, match="still open"):
        start(mini, certification_id="mini-a", count=2, seed=12, now=NOW)

    assert open_attempt(mini, "mini-a").id == sitting.id


def test_a_finished_attempt_frees_the_slot(sitting, mini: Session) -> None:
    submit(mini, sitting.id, now=NOW + timedelta(minutes=5))
    mini.commit()

    second = start(mini, certification_id="mini-a", count=2, seed=12, now=NOW)

    assert second.id != sitting.id
    assert open_attempt(mini, "mini-a").id == second.id


def test_starting_with_nothing_to_sit_refuses(mini: Session) -> None:
    with pytest.raises(AttemptError, match="nothing to sit"):
        start(mini, certification_id="no-such-cert", count=5, now=NOW)


def test_seen_ordinal_counts_earlier_exposures(sitting, mini: Session) -> None:
    submit(mini, sitting.id, now=NOW)
    mini.commit()

    second = start(mini, certification_id="mini-a", count=2, seed=12, now=NOW)
    mini.commit()

    assert {item.seen_ordinal for item in view(mini, second.id).items} == {2}


# ------------------------------------------------------------------- the option order


def test_the_recorded_permutation_is_what_gets_shown(sitting, mini: Session) -> None:
    """The criterion: the stored permutation reproduces the displayed order exactly."""
    stored = {
        item.question_id: list(item.option_order)
        for item in mini.scalars(
            sa.select(journal.AttemptItem).where(journal.AttemptItem.attempt_id == sitting.id)
        )
    }
    shown = {item.question_id: [option.label for option in item.options]
             for item in view(mini, sitting.id).items}

    assert shown == stored
    for order in stored.values():
        assert sorted(order) == ["A", "B", "C", "D"], "a permutation, not a subset"


def test_the_shuffle_actually_shuffles(mini: Session) -> None:
    """Across enough seeds the first option is not always A."""
    firsts = set()
    for seed in range(20):
        attempt = start(mini, certification_id="mini-a", count=2, seed=seed, now=NOW)
        firsts |= {order[0] for order in labels_shown(mini, attempt.id)}
        abandon(mini, attempt.id, now=NOW)
        mini.commit()

    assert firsts == {"A", "B", "C", "D"}


def test_the_whole_sitting_replays_from_the_seed(mini: Session) -> None:
    """The criterion: same questions *and* same option order, from one integer."""
    first = start(mini, certification_id="mini-a", count=2, seed=77, now=NOW)
    first_order = labels_shown(mini, first.id)
    first_ids = [item.question_id for item in view(mini, first.id).items]
    abandon(mini, first.id, now=NOW)
    mini.commit()

    second = start(mini, certification_id="mini-a", count=2, seed=77, now=NOW)

    assert [item.question_id for item in view(mini, second.id).items] == first_ids
    assert labels_shown(mini, second.id) == first_order


def test_a_different_seed_shows_a_different_order(mini: Session) -> None:
    orders = []
    for seed in (5, 6):
        attempt = start(mini, certification_id="mini-a", count=2, seed=seed, now=NOW)
        orders.append(labels_shown(mini, attempt.id))
        abandon(mini, attempt.id, now=NOW)
        mini.commit()

    assert orders[0] != orders[1]


# ------------------------------------------------------------------------- answering


def test_an_answer_is_written_when_it_is_given(sitting, mini: Session) -> None:
    answer(mini, sitting.id, 0, ["A"], time_ms=1500, now=NOW)
    mini.commit()

    item = mini.scalars(
        sa.select(journal.AttemptItem).where(
            journal.AttemptItem.attempt_id == sitting.id, journal.AttemptItem.position == 0
        )
    ).one()
    assert item.selected_labels == ["A"]
    assert item.answered_at == NOW
    assert item.time_ms == 1500


def test_changing_an_answer_counts_the_change(sitting, mini: Session) -> None:
    answer(mini, sitting.id, 0, ["A"], now=NOW)
    answer(mini, sitting.id, 0, ["C"], now=NOW + timedelta(seconds=30))
    answer(mini, sitting.id, 0, ["C"], now=NOW + timedelta(seconds=40))

    item = view(mini, sitting.id).items[0]
    assert item.selected == ("C",)
    assert item.change_count == 1, "re-sending the same answer is not a change of mind"


def test_selection_order_is_normalised(sitting, mini: Session) -> None:
    """"A then B" and "B then A" are one answer, so a change count means something."""
    position = next(
        item.position for item in view(mini, sitting.id).items if item.question_id == Q2
    )
    answer(mini, sitting.id, position, ["B", "A"], now=NOW)
    answer(mini, sitting.id, position, ["A", "B"], now=NOW + timedelta(seconds=5))

    item = view(mini, sitting.id).items[position]
    assert item.selected == ("A", "B")
    assert item.change_count == 0


def test_an_option_that_was_never_offered_is_refused(sitting, mini: Session) -> None:
    with pytest.raises(AttemptError, match="not offered"):
        answer(mini, sitting.id, 0, ["Z"], now=NOW)


def test_over_selection_is_stored_rather_than_refused(sitting, mini: Session) -> None:
    """014's truth table has a row for it, so it has to be representable.

    The runner uses radio buttons for a single-select, but the service is not the
    place to make an over-selection unrecordable -- that would leave the grading
    rule for it untestable against real data.
    """
    answer(mini, sitting.id, 0, ["A", "B"], now=NOW)

    assert view(mini, sitting.id).items[0].selected == ("A", "B")


def test_answering_a_closed_attempt_is_refused(sitting, mini: Session) -> None:
    submit(mini, sitting.id, now=NOW)
    with pytest.raises(AttemptError, match="closed"):
        answer(mini, sitting.id, 0, ["A"], now=NOW)


def test_an_unknown_attempt_or_position_says_so(sitting, mini: Session) -> None:
    with pytest.raises(AttemptError, match="no attempt"):
        answer(mini, 999, 0, ["A"])
    with pytest.raises(AttemptError, match="no item at position"):
        answer(mini, sitting.id, 99, ["A"])


# ------------------------------------------------------------ durability and resuming


def test_resuming_loses_nothing(sitting, mini: Session) -> None:
    """The criterion: killing the browser mid-attempt loses no answers.

    Modelled as the only thing a browser can actually do to a server -- stop
    talking to it. The session is closed without a submit and reopened from the
    file, which is what a resumed exam really reads.
    """
    answer(mini, sitting.id, 0, ["A"], time_ms=900, now=NOW)
    mini.commit()
    before = view(mini, sitting.id)
    engine = mini.get_bind()
    mini.close()

    with Session(engine) as resumed_session:
        resumed = view(resumed_session, sitting.id)

    assert resumed.open
    assert resumed.items[0].selected == ("A",)
    assert resumed.items[0].time_ms == 900
    assert [[o.label for o in i.options] for i in resumed.items] == [
        [o.label for o in i.options] for i in before.items
    ]


def test_an_open_attempt_is_found_again_without_being_named(sitting, mini: Session) -> None:
    assert open_attempt(mini).id == sitting.id
    assert open_attempt(mini, "mini-a").id == sitting.id
    assert open_attempt(mini, "mini-b") is None


# ------------------------------------------------------------------- the frozen snapshot


def test_the_snapshot_survives_the_corpus_changing_underneath_it(
    tmp_session: Session, tmp_kb: Path, mini_questions: list[dict]
) -> None:
    """The criterion, done by actually rebuilding the corpus underneath the attempt."""
    ingest(tmp_session, tmp_kb)
    tmp_session.commit()
    attempt = start(tmp_session, certification_id="mini-a", count=2, seed=3, now=NOW)
    answer(tmp_session, attempt.id, 0, ["A"], now=NOW)
    tmp_session.commit()
    before = view(tmp_session, attempt.id).items[0]

    rewritten = json.loads(json.dumps(mini_questions))
    for question in rewritten:
        question["prompt_md"] = "COMPLETELY DIFFERENT TEXT"
        for option in question["options"]:
            option["text_md"] = "rewritten option"
    write_kb(tmp_kb, rewritten)
    ingest(tmp_session, tmp_kb, rebuild=True)
    tmp_session.commit()

    after = view(tmp_session, attempt.id).items[0]

    assert after.prompt_md == before.prompt_md != "COMPLETELY DIFFERENT TEXT"
    assert [o.text_md for o in after.options] == [o.text_md for o in before.options]
    assert "rewritten option" not in [o.text_md for o in after.options]
    # And the projection really did change, so the test is not passing vacuously.
    assert tmp_session.scalar(
        sa.text("SELECT prompt_md FROM question WHERE id = :id"), {"id": Q1}
    ) == "COMPLETELY DIFFERENT TEXT"


def test_sqlite_itself_refuses_to_update_an_answered_snapshot(sitting, mini: Session) -> None:
    """The criterion: asserted against the database, not against the service.

    The service has no code path that updates a snapshot; this is about the day
    somebody opens the file with another tool.
    """
    answer(mini, sitting.id, 0, ["A"], now=NOW)
    mini.commit()

    with pytest.raises(IntegrityError, match="immutable"):
        mini.execute(
            sa.text(
                "UPDATE attempt_item SET snapshot_json = '{}' WHERE answered_at IS NOT NULL"
            )
        )
    mini.rollback()

    assert mini.scalars(
        sa.select(journal.AttemptItem.snapshot_json).where(
            journal.AttemptItem.attempt_id == sitting.id,
            journal.AttemptItem.position == 0,
        )
    ).one()["prompt_md"]


def test_the_option_order_is_immutable_too(sitting, mini: Session) -> None:
    answer(mini, sitting.id, 0, ["A"], now=NOW)
    mini.commit()

    with pytest.raises(IntegrityError, match="immutable"):
        mini.execute(
            sa.text("UPDATE attempt_item SET option_order = '[]' WHERE answered_at IS NOT NULL")
        )
    mini.rollback()


def test_the_service_never_writes_a_snapshot_twice(sitting, mini: Session) -> None:
    """Every write to `snapshot_json` in the service is an INSERT."""
    import inspect

    source = inspect.getsource(attempts)
    assert "snapshot_json=" in source, "it is written at insert"
    assert ".snapshot_json =" not in source, "and never assigned afterwards"
    assert "option_order=" in source and ".option_order =" not in source


# ------------------------------------------------------------- independence and finishing


def test_two_attempts_on_the_same_question_are_independent(mini: Session) -> None:
    """The criterion: neither overwrites the other."""
    first = start(mini, certification_id="mini-a", count=2, seed=1, now=NOW)
    answer(mini, first.id, 0, ["A"], now=NOW)
    submit(mini, first.id, now=NOW + timedelta(minutes=1))
    mini.commit()

    second = start(mini, certification_id="mini-a", count=2, seed=2, now=NOW)
    answer(mini, second.id, 0, ["C"], now=NOW + timedelta(days=1))
    mini.commit()

    rows = mini.execute(
        sa.select(journal.AttemptItem.attempt_id, journal.AttemptItem.selected_labels)
        .where(journal.AttemptItem.position == 0)
        .order_by(journal.AttemptItem.attempt_id)
    ).all()
    assert rows == [(first.id, ["A"]), (second.id, ["C"])]
    assert mini.scalar(sa.select(sa.func.count()).select_from(journal.AttemptItem)) == 4


def test_submitting_closes_it_and_grades_nothing(sitting, mini: Session) -> None:
    """014 owns `is_correct`. 013 must not have written one."""
    answer(mini, sitting.id, 0, ["A"], now=NOW)
    finished = submit(mini, sitting.id, now=NOW + timedelta(minutes=7))
    mini.commit()

    assert finished.submitted_at == NOW + timedelta(minutes=7)
    assert finished.elapsed_ms == 7 * 60 * 1000
    assert finished.correct_count is None and finished.credit_total is None
    assert not view(mini, sitting.id).open
    assert all(
        item.is_correct is None and item.credit is None
        for item in mini.scalars(sa.select(journal.AttemptItem))
    )


def test_finishing_twice_is_refused(sitting, mini: Session) -> None:
    submit(mini, sitting.id, now=NOW)
    with pytest.raises(AttemptError, match="already submitted"):
        submit(mini, sitting.id, now=NOW)
    with pytest.raises(AttemptError, match="already submitted"):
        abandon(mini, sitting.id, now=NOW)


def test_abandoning_leaves_submitted_at_alone(sitting, mini: Session) -> None:
    """It was not submitted, and the row must not claim it was."""
    walked = abandon(mini, sitting.id, now=NOW + timedelta(minutes=2))
    mini.commit()

    assert walked.abandoned_at == NOW + timedelta(minutes=2)
    assert walked.submitted_at is None
    assert not view(mini, sitting.id).open


# ------------------------------------------------------------------ one word, one meaning


def test_an_abandoned_attempt_reaches_no_statistic(sitting, mini: Session) -> None:
    """The criterion, against the view that defines "first exposure"."""
    answer(mini, sitting.id, 0, ["A"], now=NOW)
    abandon(mini, sitting.id, now=NOW)
    mini.commit()

    assert mini.scalar(sa.text("SELECT count(*) FROM first_exposure_response")) == 0

    second = start(mini, certification_id="mini-a", count=2, seed=9, now=NOW)
    answer(mini, second.id, 0, ["A"], now=NOW + timedelta(minutes=1))
    submit(mini, second.id, now=NOW + timedelta(minutes=2))
    mini.commit()

    rows = mini.execute(sa.text("SELECT attempt_id FROM first_exposure_response")).all()
    assert [row[0] for row in rows] == [second.id]


def test_an_abandoned_attempt_stops_blocking_chat(sitting, mini: Session) -> None:
    """Migration 0003. Before it, walking away closed chat on those questions for ever."""
    with pytest.raises(IntegrityError, match="chat is closed"):
        mini.add(journal.ChatThread(question_id=Q1, scope="browse", created_at=NOW))
        mini.flush()
    mini.rollback()

    abandon(mini, sitting.id, now=NOW)
    mini.commit()

    mini.add(journal.ChatThread(question_id=Q1, scope="browse", created_at=NOW))
    mini.flush()
    assert mini.scalar(sa.text("SELECT count(*) FROM chat_thread")) == 1


def test_submitting_also_reopens_chat(sitting, mini: Session) -> None:
    submit(mini, sitting.id, now=NOW)
    mini.commit()

    mini.add(journal.ChatThread(question_id=Q1, scope="browse", created_at=NOW))
    mini.flush()
    assert mini.scalar(sa.text("SELECT count(*) FROM chat_thread")) == 1


def test_the_service_and_the_trigger_define_open_the_same_way(mini: Session) -> None:
    """One definition, in two languages. A test is the only thing holding them together."""
    trigger = mini.scalar(
        sa.text(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'chat_thread_requires_no_open_attempt'"
        )
    )

    assert "attempt.submitted_at IS NULL" in trigger
    assert "attempt.abandoned_at IS NULL" in trigger
    for fragment in attempts.OPEN_PREDICATE.split(" AND "):
        assert fragment in trigger


# ------------------------------------------------------------------------- the reading


def test_the_view_is_built_from_snapshots_not_from_the_projection(sitting, mini: Session) -> None:
    """A resumed exam must not go back to `question`; the snapshot is the exam."""
    from sqlalchemy import event

    engine = mini.get_bind()
    seen: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def record(_conn, _cursor, statement, *_args):  # noqa: ANN001
        seen.append(statement)

    mini.commit()
    # Expire first, or `session.get()` answers from the identity map and this test
    # measures the ORM's cache rather than the read path a fresh request takes.
    mini.expire_all()
    seen.clear()
    view(mini, sitting.id)

    reads = [s for s in seen if s.lstrip().upper().startswith("SELECT")]
    assert len(reads) == 2, reads  # the attempt, then its items
    assert not any("FROM question" in s for s in reads)
    assert not any("question_option" in s for s in reads)


def test_a_missing_attempt_views_as_none(mini: Session) -> None:
    assert view(mini, 999) is None


def test_the_view_reports_progress(sitting, mini: Session) -> None:
    got = view(mini, sitting.id)
    assert not got.complete and got.answered_count == 0

    for position in range(got.count):
        answer(mini, sitting.id, position, ["A"], now=NOW)

    assert view(mini, sitting.id).complete


def test_a_disputed_question_is_flagged_on_the_item(mini: Session) -> None:
    """012 flags it in the draw; 013 records it on the item that was actually sat."""
    mini.add(
        journal.Dispute(question_id=Q1, state="open", claim_md="key looks wrong", created_at=NOW)
    )
    mini.flush()

    attempt = start(mini, certification_id="mini-a", count=2, seed=4, now=NOW)

    flagged = {item.question_id for item in view(mini, attempt.id).items if item.flagged}
    assert flagged == {Q1}
