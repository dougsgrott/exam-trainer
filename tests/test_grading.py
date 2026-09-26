"""Grading: the arithmetic that every later number rests on.

The plan settles this before any attempt exists, because changing it afterwards
invalidates every score in history. So the tests are not samples of behaviour --
they are the whole response space, enumerated:

- both question types are graded against **every** subset of their options, blank
  and all-four included, and the table is written out rather than computed, so a
  change to the rule has to be a change to a list somebody reads;
- `E[credit] = 0` is proven in `Fraction`, exactly, over the guessing space, rather
  than simulated and asserted within an epsilon;
- and the incentive properties are asserted as inequalities over that same space,
  because "partial credit rewards guessing" is a claim about a maximum, not about
  an average.

The one thing that is *not* arithmetic is the scaled score. 100-1000 and the 720
cut are published; the straight line between them is ours, and
`test_the_scale_is_data_not_code` is what keeps it replaceable.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from itertools import chain, combinations
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from examkb import db as db_module
from examkb.ingest import ingest
from examkb.models import corpus, journal
from examkb.services import grading
from examkb.services.attempts import answer, start, submit
from examkb.services.grading import (
    BLANK,
    CORRECT,
    INCORRECT,
    OVER_SELECTED,
    UNDER_SELECTED,
    GradingError,
    Key,
    Scale,
    credit_for,
    expected_credit,
    grade_attempt,
    grade_response,
    guessing_space,
    key_from_snapshot,
    scale_for,
    wrong_credit,
)

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
LABELS = ("A", "B", "C", "D")


@pytest.fixture(autouse=True)
def clean_caches():
    db_module.engine_for.cache_clear()
    yield
    db_module.engine_for.cache_clear()


def snapshot(correct: tuple[str, ...], select_count: int, labels=LABELS) -> dict:
    return {
        "question_id": "test/exam-01/q001",
        "prompt_md": "A question.",
        "type": "multi_select" if select_count > 1 else "single_select",
        "select_count": select_count,
        "correct_labels": list(correct),
        "options": [{"label": label, "text_md": f"option {label}"} for label in labels],
    }


SINGLE = snapshot(("A",), 1)
MULTI = snapshot(("A", "B"), 2)


def every_response(labels=LABELS):
    """All 16 subsets of four options, blank first."""
    return [
        tuple(subset)
        for size in range(len(labels) + 1)
        for subset in combinations(labels, size)
    ]


# ------------------------------------------------------------------ the truth table
#
# Written out, not derived. A rule change has to show up as a diff somebody reads.

SINGLE_TABLE = {
    (): (False, Fraction(0), BLANK),
    ("A",): (True, Fraction(1), CORRECT),
    ("B",): (False, Fraction(-1, 3), INCORRECT),
    ("C",): (False, Fraction(-1, 3), INCORRECT),
    ("D",): (False, Fraction(-1, 3), INCORRECT),
    ("A", "B"): (False, Fraction(-1, 3), OVER_SELECTED),
    ("B", "C"): (False, Fraction(-1, 3), OVER_SELECTED),
    ("A", "B", "C"): (False, Fraction(-1, 3), OVER_SELECTED),
    ("A", "B", "C", "D"): (False, Fraction(-1, 3), OVER_SELECTED),
}

MULTI_TABLE = {
    (): (False, Fraction(0), BLANK),
    ("A",): (False, Fraction(-1, 5), UNDER_SELECTED),
    ("C",): (False, Fraction(-1, 5), UNDER_SELECTED),
    ("A", "B"): (True, Fraction(1), CORRECT),
    ("A", "C"): (False, Fraction(-1, 5), INCORRECT),
    ("A", "D"): (False, Fraction(-1, 5), INCORRECT),
    ("B", "C"): (False, Fraction(-1, 5), INCORRECT),
    ("B", "D"): (False, Fraction(-1, 5), INCORRECT),
    ("C", "D"): (False, Fraction(-1, 5), INCORRECT),
    ("A", "B", "C"): (False, Fraction(-1, 5), OVER_SELECTED),
    ("A", "B", "C", "D"): (False, Fraction(-1, 5), OVER_SELECTED),
}


@pytest.mark.parametrize(("selected", "expected"), sorted(SINGLE_TABLE.items()))
def test_the_single_select_truth_table(selected, expected) -> None:
    is_correct, credit, outcome = expected
    grade = grade_response(SINGLE, selected)

    assert grade.is_correct is is_correct
    assert credit_for(grade.key, grade.selected) == credit
    assert grade.credit == pytest.approx(float(credit))
    assert grade.outcome == outcome


@pytest.mark.parametrize(("selected", "expected"), sorted(MULTI_TABLE.items()))
def test_the_multi_select_truth_table(selected, expected) -> None:
    is_correct, credit, outcome = expected
    grade = grade_response(MULTI, selected)

    assert grade.is_correct is is_correct
    assert credit_for(grade.key, grade.selected) == credit
    assert grade.credit == pytest.approx(float(credit))
    assert grade.outcome == outcome


@pytest.mark.parametrize("selected", every_response())
def test_every_possible_response_is_graded(selected) -> None:
    """All sixteen subsets, both types. Nothing raises and nothing is undecided."""
    for shot in (SINGLE, MULTI):
        grade = grade_response(shot, selected)
        assert grade.outcome in {CORRECT, INCORRECT, BLANK, OVER_SELECTED, UNDER_SELECTED}
        assert isinstance(grade.is_correct, bool)
        assert -1 <= grade.credit <= 1


def test_exactly_one_response_of_each_type_is_correct() -> None:
    correct = [
        selected for selected in every_response() if grade_response(MULTI, selected).is_correct
    ]
    assert correct == [("A", "B")]

    correct = [
        selected for selected in every_response() if grade_response(SINGLE, selected).is_correct
    ]
    assert correct == [("A",)]


def test_a_two_of_four_with_one_right_pick_scores_zero_not_a_half() -> None:
    """The criterion, and the reason partial credit was rejected."""
    grade = grade_response(MULTI, ("A",))

    assert grade.is_correct is False
    assert grade.credit < 0
    assert grade.credit != 0.5


def test_selection_order_does_not_matter() -> None:
    assert grade_response(MULTI, ("B", "A")).is_correct
    assert grade_response(MULTI, ("A", "B")).selected == grade_response(MULTI, ("B", "A")).selected


def test_a_label_that_was_never_offered_is_ignored() -> None:
    """013 refuses these at the door; grading must not be the second line of defence."""
    assert grade_response(MULTI, ("A", "B", "Z")).is_correct
    assert grade_response(MULTI, ("Z",)).outcome == BLANK


# --------------------------------------------------------------- the chance correction


@pytest.mark.parametrize(
    ("correct", "select_count", "responses", "wrong"),
    [
        (("A",), 1, 4, Fraction(-1, 3)),
        (("A", "B"), 2, 6, Fraction(-1, 5)),
        (("A", "B", "C"), 3, 4, Fraction(-1, 3)),  # 044's choose-THREE
    ],
)
def test_expected_credit_under_guessing_is_exactly_zero(
    correct, select_count, responses, wrong
) -> None:
    """The criterion, in rational arithmetic. Enumerated, never simulated."""
    key = key_from_snapshot(snapshot(correct, select_count))

    assert key.responses == responses
    assert wrong_credit(key) == wrong
    assert expected_credit(key) == Fraction(0)
    assert isinstance(expected_credit(key), Fraction), "exact, not a float that rounds well"

    total = sum((credit_for(key, response) for response in guessing_space(key)), Fraction(0))
    assert total == 0


def test_the_guessing_space_is_the_valid_responses_only() -> None:
    """Blank and the wrong-sized responses are outside it, deliberately."""
    key = key_from_snapshot(MULTI)
    space = guessing_space(key)

    assert len(space) == 6
    assert all(len(response) == 2 for response in space)
    assert () not in space
    assert ("A", "B", "C") not in space


def test_blank_costs_nothing() -> None:
    """The criterion: distinct from the negative credit of a confident wrong answer."""
    for shot in (SINGLE, MULTI):
        blank = grade_response(shot, ())
        wrong = grade_response(shot, ("C",) if shot is SINGLE else ("C", "D"))

        assert blank.credit == 0.0
        assert blank.outcome == BLANK
        assert wrong.credit < blank.credit


def test_hedging_never_pays() -> None:
    """The criterion: no response beats an honest guess.

    Selecting everything cannot be exactly right, so it earns the wrong-answer
    credit -- strictly worse than blank and no better than guessing. If it scored
    zero, "tick all four" would be the optimal play for someone who knows nothing.
    """
    for shot in (SINGLE, MULTI):
        key = key_from_snapshot(shot)
        everything = grade_response(shot, LABELS)
        blank = grade_response(shot, ())
        guess_expectation = float(expected_credit(key))

        assert everything.credit == float(wrong_credit(key))
        assert everything.credit < blank.credit
        assert everything.credit <= guess_expectation


def test_partial_knowledge_is_rewarded() -> None:
    """Knowing one of the two and guessing the other must beat knowing nothing.

    A correction that flattened this would make the mastery model blind to the
    difference between half-knowing and not knowing.
    """
    key = key_from_snapshot(MULTI)
    informed = [response for response in guessing_space(key) if "A" in response]

    expectation = sum((credit_for(key, r) for r in informed), Fraction(0)) / len(informed)

    assert expectation == Fraction(1, 5)
    assert expectation > expected_credit(key) == 0


def test_the_credit_range_is_minus_one_over_c_to_one() -> None:
    for select_count in (1, 2, 3):
        key = key_from_snapshot(snapshot(tuple(LABELS[:select_count]), select_count))
        credits = [credit_for(key, response) for response in guessing_space(key)]

        assert max(credits) == Fraction(1)
        assert min(credits) == wrong_credit(key)


# ------------------------------------------------------------------------ purity


def test_grading_a_response_needs_no_database() -> None:
    """The criterion: a pure function of (snapshot, response)."""
    parameters = inspect.signature(grade_response).parameters

    assert "session" not in parameters
    assert list(parameters) == ["snapshot", "selected"]
    # And it really runs with nothing else in scope.
    assert grade_response(SINGLE, ("A",)).is_correct


def test_a_snapshot_with_no_key_refuses_rather_than_marking_everything_wrong() -> None:
    with pytest.raises(GradingError, match="no correct answer"):
        grade_response({"question_id": "x", "options": [{"label": "A"}], "correct_labels": []}, ())
    with pytest.raises(GradingError, match="no options"):
        grade_response({"question_id": "x", "options": [], "correct_labels": ["A"]}, ())


def test_the_key_comes_from_the_snapshot_not_from_the_corpus() -> None:
    """040 overriding a key today must not change what last year was marked against."""
    frozen = snapshot(("A",), 1)
    frozen["correct_labels"] = ["C"]  # what the exam said at the time

    assert grade_response(frozen, ("C",)).is_correct
    assert not grade_response(frozen, ("A",)).is_correct


# -------------------------------------------------------------------------- the scale


def test_the_published_endpoints_and_cut() -> None:
    scale = grading.DEFAULT_SCALES["ccao-f"]

    assert (scale.scale_min, scale.scale_max, scale.cut_score) == (100, 1000, 720)
    assert scale.scaled(0.0) == 100
    assert scale.scaled(1.0) == 1000
    assert scale.cut_proportion == pytest.approx(620 / 900)


@pytest.mark.parametrize(
    ("correct", "of", "expected", "passes"),
    [(60, 60, 1000, True), (42, 60, 730, True), (41, 60, 715, False), (0, 60, 100, False)],
)
def test_the_cut_falls_where_the_arithmetic_says(correct, of, expected, passes) -> None:
    scale = grading.DEFAULT_SCALES["ccao-f"]
    scaled = scale.scaled(correct / of)

    assert scaled == expected
    assert scale.passed(scaled) is passes


def test_a_proportion_outside_zero_to_one_is_clamped() -> None:
    scale = grading.DEFAULT_SCALES["ccao-f"]
    assert scale.scaled(-1.0) == 100
    assert scale.scaled(2.0) == 1000


def test_the_scale_is_data_not_code(tmp_session: Session, tmp_kb: Path) -> None:
    """The criterion: changing a cut score is a row, not a commit."""
    ingest(tmp_session, tmp_kb)
    tmp_session.commit()

    assert scale_for(tmp_session, "mini-a").cut_score == 720
    assert scale_for(tmp_session, "mini-a").source == "default"

    run_id = tmp_session.scalar(sa.select(corpus.IngestRun.id).limit(1))
    tmp_session.add(
        corpus.FormatProfile(
            id="mini-a",
            certification_id="mini-a",
            scale_min=200,
            scale_max=800,
            cut_score=500,
            item_count=40,
            source="vendor-handbook",
            ingest_run_id=run_id,
        )
    )
    tmp_session.flush()

    scale = scale_for(tmp_session, "mini-a")
    assert (scale.scale_min, scale.scale_max, scale.cut_score) == (200, 800, 500)
    assert scale.source == "vendor-handbook"
    assert scale.scaled(0.5) == 500 and scale.passed(500)


def test_an_unknown_certification_gets_the_generic_scale(tmp_session: Session) -> None:
    scale = scale_for(tmp_session, "who-knows")

    assert (scale.scale_min, scale.scale_max, scale.cut_score) == (100, 1000, 720)
    assert scale.source == "default"


def test_the_scaled_score_says_it_is_a_model() -> None:
    """The endpoints are published; the straight line between them is ours."""
    assert "linear" in grading.Scale.scaled.__doc__.lower()
    assert grading.DEFAULT_SCALES["ccao-f"].source == "published"


# ------------------------------------------------------------------- grading an attempt


@pytest.fixture
def mini(tmp_session: Session, tmp_kb: Path) -> Session:
    ingest(tmp_session, tmp_kb)
    tmp_session.commit()
    return tmp_session


def test_grading_an_attempt_writes_both_numbers(mini: Session) -> None:
    attempt = start(mini, certification_id="mini-a", count=2, seed=1, now=NOW)
    view_items = mini.scalars(
        sa.select(journal.AttemptItem).order_by(journal.AttemptItem.position)
    ).all()
    for item in view_items:
        answer(mini, attempt.id, item.position, item.snapshot_json["correct_labels"], now=NOW)
    submit(mini, attempt.id, now=NOW + timedelta(minutes=5))
    mini.commit()

    result = grade_attempt(mini, attempt.id, now=NOW + timedelta(minutes=6))
    mini.commit()

    assert result.correct_count == result.item_count == 2
    assert result.credit_total == pytest.approx(2.0)
    assert result.scaled_score == 1000 and result.passed
    assert "2/2 correct (100%)" in result.summary()
    assert all(item.is_correct and item.credit == 1.0 for item in view_items)
    assert attempt.graded_at is not None


def test_a_blank_attempt_scores_the_floor(mini: Session) -> None:
    attempt = start(mini, certification_id="mini-a", count=2, seed=1, now=NOW)
    submit(mini, attempt.id, now=NOW + timedelta(minutes=1))
    mini.commit()

    result = grade_attempt(mini, attempt.id, now=NOW)

    assert result.correct_count == 0
    assert result.credit_total == 0.0, "blank costs nothing, so a blank exam is zero not negative"
    assert result.scaled_score == 100 and not result.passed
    assert result.answered_count == 0


def test_a_wrong_attempt_scores_below_a_blank_one(mini: Session) -> None:
    attempt = start(mini, certification_id="mini-a", count=2, seed=1, now=NOW)
    for item in mini.scalars(sa.select(journal.AttemptItem)):
        wrong = [
            option["label"]
            for option in item.snapshot_json["options"]
            if option["label"] not in item.snapshot_json["correct_labels"]
        ][: item.snapshot_json["select_count"]]
        answer(mini, attempt.id, item.position, wrong, now=NOW)
    submit(mini, attempt.id, now=NOW + timedelta(minutes=1))
    mini.commit()

    result = grade_attempt(mini, attempt.id, now=NOW)

    assert result.correct_count == 0
    assert result.credit_total < 0
    assert result.scaled_score == 100


def test_grading_twice_returns_what_is_stored(mini: Session) -> None:
    """The criterion: 040 owns the only path that changes a stored grade."""
    attempt = start(mini, certification_id="mini-a", count=2, seed=1, now=NOW)
    first_item = mini.scalars(
        sa.select(journal.AttemptItem).order_by(journal.AttemptItem.position)
    ).first()
    answer(mini, attempt.id, 0, first_item.snapshot_json["correct_labels"], now=NOW)
    submit(mini, attempt.id, now=NOW + timedelta(minutes=1))
    mini.commit()

    first = grade_attempt(mini, attempt.id, now=NOW)
    mini.commit()
    again = grade_attempt(mini, attempt.id, now=NOW + timedelta(days=1))

    assert not first.already_graded and again.already_graded
    assert again.correct_count == first.correct_count
    assert again.scaled_score == first.scaled_score
    assert again.graded_at == first.graded_at, "the second call did not restamp it"


def test_an_unsubmitted_attempt_cannot_be_graded(mini: Session) -> None:
    attempt = start(mini, certification_id="mini-a", count=2, seed=1, now=NOW)
    mini.commit()

    with pytest.raises(GradingError, match="not been submitted"):
        grade_attempt(mini, attempt.id, now=NOW)


def test_an_unknown_attempt_says_so(mini: Session) -> None:
    with pytest.raises(GradingError, match="no attempt"):
        grade_attempt(mini, 999, now=NOW)


def test_the_grade_uses_the_frozen_key_after_the_corpus_moves(
    tmp_session: Session, tmp_kb: Path, mini_questions: list[dict]
) -> None:
    """013 froze the key; 014 marks against it, whatever `question` says now."""
    import json

    from conftest import write_kb

    ingest(tmp_session, tmp_kb)
    tmp_session.commit()
    attempt = start(tmp_session, certification_id="mini-a", count=2, seed=1, now=NOW)
    for item in tmp_session.scalars(sa.select(journal.AttemptItem)):
        answer(tmp_session, attempt.id, item.position, item.snapshot_json["correct_labels"], now=NOW)
    submit(tmp_session, attempt.id, now=NOW)
    tmp_session.commit()

    moved = json.loads(json.dumps(mini_questions))
    for question in moved:
        question["correct_labels"] = ["D"]
        for option in question["options"]:
            option["correct"] = option["label"] == "D"
    write_kb(tmp_kb, moved)
    ingest(tmp_session, tmp_kb, rebuild=True)
    tmp_session.commit()

    result = grade_attempt(tmp_session, attempt.id, now=NOW)

    assert result.correct_count == 2, "marked against the key the exam actually asked"
    # And the corpus really did move, so this is not passing vacuously.
    live = json.loads(tmp_session.scalar(sa.text("SELECT correct_labels FROM question LIMIT 1")))
    assert live == ["D"]


# ------------------------------------------------------------------ against the corpus


def test_the_corpus_only_contains_the_two_shapes_this_grader_knows(real_session: Session) -> None:
    """Which is why there are exactly two constants in play: -1/3 and -1/5."""
    rows = real_session.execute(
        sa.text(
            "SELECT question.type, question.select_count, count(DISTINCT option_count.n) "
            "FROM question JOIN (SELECT question_id, count(*) n FROM question_option "
            "GROUP BY question_id) AS option_count ON option_count.question_id = question.id "
            "GROUP BY 1, 2"
        )
    ).all()

    assert sorted((row[0], row[1]) for row in rows) == [
        ("multi_select", 2),
        ("single_select", 1),
    ]

    options = real_session.scalar(
        sa.text("SELECT count(DISTINCT n) FROM (SELECT count(*) n FROM question_option "
                "GROUP BY question_id)")
    )
    assert options == 1, "every question has the same number of options"
