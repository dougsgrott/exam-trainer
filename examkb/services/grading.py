"""Scoring an attempt: one number for the person, one for the model.

The plan settles this before any attempt exists, because changing it later
invalidates every score in history. There are two numbers and they answer
different questions:

**`is_correct`** is strict exact match, and it is what the results page shows. A
2-of-4 with one right pick is wrong, not half right. Partial credit on a
multi-select rewards guessing -- pick any two of four and you are "half right"
five times in six -- and a score that goes up when you know nothing is not a score.

**`credit`** is the same response, chance-corrected, and it is what the mastery
model (020) reads. It is calibrated so that **a uniform guesser earns exactly
zero**: right is worth `+1`, wrong is worth `-1 / (C(n, s) - 1)`, and there are
`C(n, s) - 1` ways to be wrong. For 1-of-4 that is `-1/3`; for 2-of-4, `-1/5`.
The arithmetic is done in `Fraction` so "exactly zero" is a fact rather than a
float that rounds well.

Three responses sit outside the guessing space, and each is decided rather than
defaulted:

- **Blank earns 0.** Not answering is neither knowledge nor a guess. It must not
  cost anything, or the model punishes running out of time.
- **Over-selection earns the wrong-answer credit**, not zero. If hedging scored 0
  it would beat guessing, and "select all four" would be the optimal play for
  someone who knows nothing. `test_hedging_never_pays` is that argument as a test.
- **Under-selection likewise.** Picking one of two required is a response, not an
  abstention; it says something, and what it says is wrong.

`grade_response` is pure -- snapshot in, grade out, no session -- so the truth
table can be enumerated rather than sampled. `grade_attempt` is the half that
touches the database, and it never rewrites a grade that is already there: 040's
explicit regrade is the only path that changes history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from itertools import combinations
from math import comb

import sqlalchemy as sa
from sqlalchemy.orm import Session

from examkb.models import corpus, journal
from examkb.models.base import utcnow

# What a response was, beyond right or wrong. 016 shows these; nothing branches on
# them, so adding one is a display change rather than a scoring change.
CORRECT = "correct"
INCORRECT = "incorrect"
BLANK = "blank"
OVER_SELECTED = "over_selected"
UNDER_SELECTED = "under_selected"


class GradingError(ValueError):
    """A snapshot that cannot be graded, or an attempt that must not be."""


# ------------------------------------------------------------------------ the scale
#
# 005 put `scale_min`, `scale_max` and `cut_score` on `format_profile` and said in
# its docstring that the 720 cut lives there rather than here. The table is a
# PROJECTION table that nothing fills yet -- 044 owns that -- so these are the
# published figures as a fallback, and a row overrides them without a code change.


@dataclass(frozen=True)
class Scale:
    """How a proportion correct becomes the number on the results page."""

    certification_id: str
    scale_min: int = 100
    scale_max: int = 1000
    cut_score: int = 720
    item_count: int | None = None
    source: str = "published"

    @property
    def cut_proportion(self) -> float:
        """The fraction of items the cut corresponds to under this linear map."""
        span = self.scale_max - self.scale_min
        return (self.cut_score - self.scale_min) / span if span else 0.0

    def scaled(self, proportion: float) -> int:
        """Proportion correct -> scaled score.

        A **linear** map, and that is a model rather than a measurement: real
        certification scaling is equated per form and the vendor does not publish
        theirs. The endpoints and the cut are the published ones; the line between
        them is ours, and `source` is what lets a page say so instead of implying
        an official number.
        """
        proportion = min(1.0, max(0.0, proportion))
        span = self.scale_max - self.scale_min
        return int(round(self.scale_min + proportion * span))

    def passed(self, scaled_score: int) -> bool:
        return scaled_score >= self.cut_score


# Both Anthropic certifications: 100-1000 scaled, cut 720, 12-month validity.
# CCAO-F is 60 items / 120 min and CCAR-P 63 / 120 (the plan's table).
DEFAULT_SCALES: dict[str, Scale] = {
    "ccao-f": Scale("ccao-f", 100, 1000, 720, item_count=60, source="published"),
    "ccar-p": Scale("ccar-p", 100, 1000, 720, item_count=63, source="published"),
}
GENERIC_SCALE = Scale("", 100, 1000, 720, source="default")


def scale_for(session: Session, certification_id: str) -> Scale:
    """The scale for a certification: the `format_profile` row, or the default.

    A row wins, which is what "changing a cut score does not require a code change"
    means in practice -- and what 044 will populate from `kb/`.
    """
    row = session.execute(
        sa.select(
            corpus.FormatProfile.scale_min,
            corpus.FormatProfile.scale_max,
            corpus.FormatProfile.cut_score,
            corpus.FormatProfile.item_count,
            corpus.FormatProfile.source,
        ).where(corpus.FormatProfile.certification_id == certification_id)
    ).first()

    fallback = DEFAULT_SCALES.get(certification_id, GENERIC_SCALE)
    if row is None:
        return Scale(
            certification_id=certification_id,
            scale_min=fallback.scale_min,
            scale_max=fallback.scale_max,
            cut_score=fallback.cut_score,
            item_count=fallback.item_count,
            source=fallback.source,
        )

    scale_min, scale_max, cut_score, item_count, source = row
    return Scale(
        certification_id=certification_id,
        scale_min=int(scale_min if scale_min is not None else fallback.scale_min),
        scale_max=int(scale_max if scale_max is not None else fallback.scale_max),
        cut_score=int(cut_score if cut_score is not None else fallback.cut_score),
        item_count=item_count if item_count is not None else fallback.item_count,
        source=source or "format_profile",
    )


# ---------------------------------------------------------------------- the truth table


@dataclass(frozen=True)
class Key:
    """Everything grading one response needs, and nothing else."""

    correct_labels: tuple[str, ...]
    option_labels: tuple[str, ...]
    select_count: int

    @property
    def options(self) -> int:
        return len(self.option_labels)

    @property
    def responses(self) -> int:
        """How many responses of the right size there are. The guessing space."""
        return comb(self.options, self.select_count)


@dataclass(frozen=True)
class Grade:
    is_correct: bool
    credit: float
    outcome: str
    selected: tuple[str, ...]
    key: Key

    @property
    def blank(self) -> bool:
        return self.outcome == BLANK


def key_from_snapshot(snapshot: dict) -> Key:
    """The key as the exam asked it, from `attempt_item.snapshot_json`.

    From the snapshot, never from `current_answer_key`: the snapshot is what was
    on the screen, and 040 overriding a key today must not change what a sitting
    from last year was marked against. That is the whole point of 013 freezing it.
    """
    options = tuple(option["label"] for option in snapshot.get("options", ()))
    if not options:
        raise GradingError(f"snapshot for {snapshot.get('question_id')!r} has no options")

    correct = tuple(label for label in options if label in set(snapshot.get("correct_labels", ())))
    if not correct:
        raise GradingError(
            f"snapshot for {snapshot.get('question_id')!r} has no correct answer to mark against"
        )

    select_count = int(snapshot.get("select_count") or len(correct))
    return Key(correct_labels=correct, option_labels=options, select_count=select_count)


def wrong_credit(key: Key) -> Fraction:
    """`-1 / (C(n, s) - 1)`: the exact value that makes a guesser break even.

    There is one right response and `C(n, s) - 1` wrong ones, so a uniform guesser
    earns `1/C * 1 + (C-1)/C * -1/(C-1)`, which is zero. `Fraction`, because a test
    asserts *exactly* zero and `3 * (1/3)` is only accidentally 1.0 in binary.
    """
    wrong_ways = key.responses - 1
    if wrong_ways <= 0:  # one possible response: there is nothing to be wrong about
        return Fraction(0)
    return Fraction(-1, wrong_ways)


def credit_for(key: Key, selected: tuple[str, ...]) -> Fraction:
    """The exact credit for one response. `float()` of this is what gets stored."""
    if not selected:
        return Fraction(0)
    if set(selected) == set(key.correct_labels) and len(selected) == len(key.correct_labels):
        return Fraction(1)
    return wrong_credit(key)


def outcome_for(key: Key, selected: tuple[str, ...]) -> str:
    if not selected:
        return BLANK
    if set(selected) == set(key.correct_labels) and len(selected) == len(key.correct_labels):
        return CORRECT
    if len(selected) > key.select_count:
        return OVER_SELECTED
    if len(selected) < key.select_count:
        return UNDER_SELECTED
    return INCORRECT


def grade_response(snapshot: dict, selected) -> Grade:
    """Grade one answer. Pure: a snapshot and a response in, a grade out."""
    key = key_from_snapshot(snapshot)
    chosen = tuple(
        label for label in key.option_labels if label in set(selected or ())
    )
    return Grade(
        is_correct=set(chosen) == set(key.correct_labels) and len(chosen) == len(key.correct_labels),
        credit=float(credit_for(key, chosen)),
        outcome=outcome_for(key, chosen),
        selected=chosen,
        key=key,
    )


def guessing_space(key: Key) -> list[tuple[str, ...]]:
    """Every response a uniform guesser could give: the `C(n, s)` valid subsets.

    Blank and the wrong-sized responses are deliberately not in here. A guesser
    follows the instruction on the screen -- "Select TWO" -- and the correction is
    calibrated against that. The responses outside this space are decided in the
    module docstring, not averaged into it.
    """
    return [
        tuple(subset) for subset in combinations(key.option_labels, key.select_count)
    ]


def expected_credit(key: Key) -> Fraction:
    """The exact expectation over `guessing_space(key)`. Zero, and provably so."""
    space = guessing_space(key)
    if not space:  # pragma: no cover -- C(n, s) is at least 1 for s <= n
        return Fraction(0)
    return sum((credit_for(key, response) for response in space), Fraction(0)) / len(space)


# --------------------------------------------------------------------------- persistence


@dataclass(frozen=True)
class AttemptGrade:
    """What the results page (016) needs, and what got written."""

    attempt_id: int
    certification_id: str
    item_count: int
    answered_count: int
    correct_count: int
    credit_total: float
    scaled_score: int
    passed: bool
    scale: Scale
    graded_at: datetime
    already_graded: bool = False

    @property
    def proportion(self) -> float:
        return self.correct_count / self.item_count if self.item_count else 0.0

    @property
    def percent(self) -> float:
        return self.proportion * 100

    def summary(self) -> str:
        verdict = "pass" if self.passed else "fail"
        return (
            f"{self.correct_count}/{self.item_count} correct "
            f"({self.percent:.0f}%) -- scaled {self.scaled_score}, "
            f"cut {self.scale.cut_score}: {verdict}"
        )


def grade_attempt(
    session: Session, attempt_id: int, *, now: datetime | None = None
) -> AttemptGrade:
    """Grade a submitted attempt, once. Calling it again returns what is stored.

    Idempotent rather than refusing, because the results page is a URL somebody
    reloads. Idempotent is not the same as re-gradable: nothing here overwrites an
    `is_correct` that already exists, and 040's explicit regrade is the only path
    that changes a stored grade.
    """
    attempt = session.get(journal.Attempt, attempt_id)
    if attempt is None:
        raise GradingError(f"no attempt {attempt_id}")
    if attempt.submitted_at is None:
        raise GradingError(
            f"attempt {attempt_id} has not been submitted; there is nothing to grade"
        )

    scale = scale_for(session, attempt.certification_id)

    if attempt.graded_at is not None:
        return AttemptGrade(
            attempt_id=attempt.id,
            certification_id=attempt.certification_id,
            item_count=attempt.item_count,
            answered_count=_answered(session, attempt_id),
            correct_count=attempt.correct_count or 0,
            credit_total=float(attempt.credit_total or 0.0),
            scaled_score=attempt.scaled_score or scale.scale_min,
            passed=bool(attempt.passed),
            scale=scale,
            graded_at=attempt.graded_at,
            already_graded=True,
        )

    now = now or utcnow()
    items = session.scalars(
        sa.select(journal.AttemptItem)
        .where(journal.AttemptItem.attempt_id == attempt_id)
        .order_by(journal.AttemptItem.position)
    ).all()

    correct_count = 0
    credit_total = Fraction(0)
    answered = 0
    for item in items:
        grade = grade_response(item.snapshot_json, item.selected_labels or ())
        item.is_correct = grade.is_correct
        item.credit = grade.credit
        item.graded_at = now
        correct_count += int(grade.is_correct)
        credit_total += credit_for(grade.key, grade.selected)
        answered += int(item.answered_at is not None)

    proportion = correct_count / len(items) if items else 0.0
    scaled_score = scale.scaled(proportion)

    attempt.correct_count = correct_count
    attempt.credit_total = float(credit_total)
    attempt.scaled_score = scaled_score
    attempt.passed = scale.passed(scaled_score)
    attempt.graded_at = now
    session.flush()

    return AttemptGrade(
        attempt_id=attempt.id,
        certification_id=attempt.certification_id,
        item_count=len(items),
        answered_count=answered,
        correct_count=correct_count,
        credit_total=float(credit_total),
        scaled_score=scaled_score,
        passed=bool(attempt.passed),
        scale=scale,
        graded_at=now,
    )


def _answered(session: Session, attempt_id: int) -> int:
    return int(
        session.scalar(
            sa.select(sa.func.count())
            .select_from(journal.AttemptItem)
            .where(
                journal.AttemptItem.attempt_id == attempt_id,
                journal.AttemptItem.answered_at.isnot(None),
            )
        )
        or 0
    )
