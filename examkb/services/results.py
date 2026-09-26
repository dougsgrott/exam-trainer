"""What a finished exam looks like.

The session seam for `/results`, the way `browse.py` and `runner.py` are for their
pages. Everything it returns is built from `attempt_item.snapshot_json` and the
stored permutation, never from `question`: re-opening a sitting after an
`ingest --rebuild` has to show the exam that was sat, not the corpus as it is now.
That is the whole reason 013 froze it.

Grading happens here, on first view. 014 made `grade_attempt` idempotent precisely
so this page could be a URL somebody reloads; the second visit reads what the first
one wrote and re-stamps nothing.

**An open attempt has no results.** The snapshot holds the answer key, and a
results URL that worked mid-exam would make 015's "the runner never shows the key"
worth nothing -- you would just open the other tab.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from examkb import db
from examkb.models import journal
from examkb.services import grading
from examkb.services.grading import AttemptGrade, GradingError


class ResultsNotReady(RuntimeError):
    """The attempt is still open. There is nothing to show and a key not to leak."""


@dataclass(frozen=True)
class ResultOption:
    label: str
    text_md: str
    selected: bool
    correct: bool
    explanation_md: str | None

    @property
    def state(self) -> str:
        """How the option is marked up: the four combinations, named once."""
        if self.correct and self.selected:
            return "right"
        if self.correct:
            return "missed"
        if self.selected:
            return "wrong"
        return "plain"


@dataclass(frozen=True)
class ResultItem:
    """One question as it was asked, with what happened to it.

    Unlike the runner's `ItemView` this deliberately carries the key and every
    explanation -- that is what the page is for, and what 041 builds its chat
    context from.
    """

    position: int
    question_id: str
    prompt_md: str
    options: tuple[ResultOption, ...]
    overall_explanation_md: str | None
    domain_label: str | None
    exam_id: str | None
    multi: bool
    select_count: int
    selected: tuple[str, ...]
    correct_labels: tuple[str, ...]
    is_correct: bool
    credit: float
    outcome: str
    seen_ordinal: int | None
    time_ms: int
    change_count: int
    disputed: bool

    @property
    def number(self) -> int:
        return self.position + 1

    @property
    def blank(self) -> bool:
        return not self.selected


@dataclass(frozen=True)
class DomainRow:
    """A per-domain line. The count is not optional -- see `share`."""

    label: str
    correct: int
    asked: int

    @property
    def percent(self) -> float:
        return 100.0 * self.correct / self.asked if self.asked else 0.0

    @property
    def share(self) -> str:
        """Always "c of n", and the percent only ever beside it.

        The plan's rule is that no statistic renders without its `n`. A domain with
        two questions in it must not say "50%" on its own -- 1 of 2 is a fact and
        50% is an invitation to read it as an ability.
        """
        return f"{self.correct} of {self.asked}"


@dataclass(frozen=True)
class ResultsPage:
    grade: AttemptGrade
    items: tuple[ResultItem, ...]
    domains: tuple[DomainRow, ...]
    started_at: datetime
    submitted_at: datetime | None
    elapsed_ms: int | None

    @property
    def attempt_id(self) -> int:
        return self.grade.attempt_id

    @property
    def blank_count(self) -> int:
        return sum(1 for item in self.items if item.blank)

    @property
    def elapsed_label(self) -> str:
        if not self.elapsed_ms:
            return "—"
        total = self.elapsed_ms // 1000
        return f"{total // 3600}h {total // 60 % 60:02d}m" if total >= 3600 else f"{total // 60}m {total % 60:02d}s"

    @property
    def chart_items(self) -> list[tuple[str, float]]:
        """`(label, percent)` for the bar chart. The counts ride in the table."""
        return [(f"{row.label} ({row.asked})", row.percent) for row in self.domains]

    @property
    def chart_rows(self) -> list[list[str]]:
        return [
            [row.label, str(row.correct), str(row.asked), f"{row.percent:.0f}%"]
            for row in self.domains
        ]


def _item(row: journal.AttemptItem) -> ResultItem:
    snapshot = row.snapshot_json
    by_label = {option["label"]: option for option in snapshot["options"]}
    selected = tuple(row.selected_labels or ())
    correct = tuple(snapshot.get("correct_labels") or ())
    grade = grading.grade_response(snapshot, selected)

    return ResultItem(
        position=row.position,
        question_id=row.question_id,
        prompt_md=snapshot["prompt_md"],
        options=tuple(
            ResultOption(
                label=label,
                text_md=by_label[label]["text_md"],
                selected=label in selected,
                correct=label in correct,
                explanation_md=by_label[label].get("explanation_md"),
            )
            # The *stored* permutation, so the replay reads the way the exam did.
            for label in row.option_order
            if label in by_label
        ),
        overall_explanation_md=snapshot.get("overall_explanation_md"),
        domain_label=snapshot.get("domain_label"),
        exam_id=snapshot.get("exam_id"),
        multi=snapshot.get("type") == "multi_select",
        select_count=int(snapshot.get("select_count") or 1),
        selected=selected,
        correct_labels=correct,
        is_correct=bool(row.is_correct),
        credit=float(row.credit or 0.0),
        outcome=grade.outcome,
        seen_ordinal=row.seen_ordinal,
        time_ms=row.time_ms,
        change_count=row.change_count,
        disputed=snapshot.get("flagged") == "disputed",
    )


def _domains(items: tuple[ResultItem, ...]) -> tuple[DomainRow, ...]:
    asked: dict[str, int] = {}
    correct: dict[str, int] = {}
    for item in items:
        label = item.domain_label or "unspecified"
        asked[label] = asked.get(label, 0) + 1
        correct[label] = correct.get(label, 0) + int(item.is_correct)
    return tuple(
        DomainRow(label=label, correct=correct[label], asked=count)
        # Worst first: the page is for finding what to study.
        for label, count in sorted(
            asked.items(), key=lambda pair: (correct[pair[0]] / pair[1], pair[0])
        )
    )


def page(
    attempt_id: int, *, url: str | None = None, now: datetime | None = None
) -> ResultsPage | None:
    """Grade if needed, then assemble. Returns None when there is no such attempt."""
    if not db.projection_ready(url):
        return None
    with db.session_for(url) as session:
        attempt = session.get(journal.Attempt, attempt_id)
        if attempt is None:
            return None
        if attempt.submitted_at is None and attempt.abandoned_at is None:
            raise ResultsNotReady(
                f"attempt {attempt_id} is still open; there are no results until it is submitted"
            )
        if attempt.submitted_at is None:
            raise ResultsNotReady(
                f"attempt {attempt_id} was abandoned; an abandoned attempt is not scored"
            )

        grade = grading.grade_attempt(session, attempt_id, now=now)
        rows = session.scalars(
            sa.select(journal.AttemptItem)
            .where(journal.AttemptItem.attempt_id == attempt_id)
            .order_by(journal.AttemptItem.position)
        ).all()
        items = tuple(_item(row) for row in rows)
        session.commit()

        return ResultsPage(
            grade=grade,
            items=items,
            domains=_domains(items),
            started_at=attempt.started_at,
            submitted_at=attempt.submitted_at,
            elapsed_ms=attempt.elapsed_ms,
        )


def recent(limit: int = 20, *, url: str | None = None) -> list[tuple[int, str, datetime, bool]]:
    """`(id, certification, submitted_at, passed)` for the finished sittings."""
    if not db.projection_ready(url):
        return []
    with db.session_for(url) as session:
        rows = session.execute(
            sa.select(
                journal.Attempt.id,
                journal.Attempt.certification_id,
                journal.Attempt.submitted_at,
                journal.Attempt.passed,
            )
            .where(journal.Attempt.submitted_at.isnot(None))
            .order_by(journal.Attempt.submitted_at.desc())
            .limit(limit)
        ).all()
        return [(row[0], row[1], row[2], bool(row[3])) for row in rows]


__all__ = [
    "AttemptGrade",
    "DomainRow",
    "GradingError",
    "ResultItem",
    "ResultOption",
    "ResultsNotReady",
    "ResultsPage",
    "page",
    "recent",
]
