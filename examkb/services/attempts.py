"""Starting, sitting, and finishing an exam.

An attempt is the only record in this repo that cannot be rebuilt from anything.
`kb/` regenerates from `data/`, the projection regenerates from `kb/`, marks can at
least be re-marked -- an attempt is a thing that happened once. Three rules follow
and this module is all three:

1. **The exam is frozen when it starts.** Every item's `snapshot_json` is the
   question as it was at that moment, key and explanations included, and its
   `option_order` is the permutation it will be shown in. Nothing here ever updates
   either; a trigger (005) refuses it as well, for the day somebody writes SQL by
   hand. That is what lets 016 replay a sitting years later, after
   `ingest --rebuild` has replaced the corpus text and 040 has overridden the key.

2. **An answer is durable when it is given.** `answer()` writes one row's selection
   and the caller commits; the web layer commits per answer rather than at submit,
   so closing the laptop mid-exam loses the last few seconds and nothing else.

3. **"Open" has one definition** -- started, not submitted, not abandoned -- and it
   is used here, by the chat gate (0003) and by `first_exposure_response`. When
   those three disagreed, an abandoned exam blocked chat about its questions for
   ever; that is what migration 0003 is.

The shuffle is derived from `attempt.seed`, not from the clock, so the whole
sitting -- which questions, in what order, with options in what order -- replays
from one integer.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from examkb import queries
from examkb.models import journal
from examkb.models.base import utcnow
from examkb.services import sampler
from examkb.services.sampler import Draw
from examkb.services.search import SearchFilters


class AttemptError(RuntimeError):
    """The lifecycle was asked for something it will not do."""


# `attempt.submitted_at IS NULL AND attempt.abandoned_at IS NULL`, in one place.
# Written as SQL because the chat trigger (0003) has to say the same thing in DDL
# and a test compares the two.
OPEN_PREDICATE = "attempt.submitted_at IS NULL AND attempt.abandoned_at IS NULL"


def _is_open(attempt: journal.Attempt) -> bool:
    return attempt.submitted_at is None and attempt.abandoned_at is None


# --------------------------------------------------------------------------- the views
#
# Plain dataclasses, because 015 renders them and `examkb/web/` may not hold an ORM
# object (008's layering rule). They are built from the *snapshot*, never from the
# live projection -- that is the whole point of having taken one.


@dataclass(frozen=True)
class OptionView:
    label: str
    text_md: str

    @property
    def key(self) -> str:
        """What a form posts back. The vendor's letter, not the display position."""
        return self.label


@dataclass(frozen=True)
class ItemView:
    position: int
    question_id: str
    prompt_md: str
    options: tuple[OptionView, ...]
    type: str
    select_count: int
    domain_label: str | None
    selected: tuple[str, ...] = ()
    answered: bool = False
    flagged: bool = False
    time_ms: int = 0
    change_count: int = 0
    seen_ordinal: int | None = None

    @property
    def multi(self) -> bool:
        return self.type == "multi_select"


@dataclass(frozen=True)
class AttemptView:
    id: int
    certification_id: str
    started_at: datetime
    submitted_at: datetime | None
    abandoned_at: datetime | None
    seed: int
    items: tuple[ItemView, ...]
    time_limit_seconds: int | None = None

    @property
    def open(self) -> bool:
        return self.submitted_at is None and self.abandoned_at is None

    @property
    def answered_count(self) -> int:
        return sum(1 for item in self.items if item.answered)

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def complete(self) -> bool:
        return self.answered_count == self.count


# ----------------------------------------------------------------------- the snapshot


def snapshot_of(detail: queries.QuestionDetail) -> dict:
    """The question as it is right now, in the shape a replay will need.

    The answer key and the explanations are in here even though the runner does not
    show them during the exam. The snapshot is not "the pixels"; it is the state of
    the question at the moment it was asked, and 016 has to be able to say what was
    correct *then* after 040 has overridden what is correct now.
    """
    return {
        "question_id": detail.id,
        "prompt_md": detail.prompt_md,
        "overall_explanation_md": detail.overall_explanation_md,
        "type": detail.type,
        "select_count": detail.select_count,
        "correct_labels": list(detail.correct_labels),
        "certification_id": detail.certification_id,
        "exam_id": detail.exam_id,
        "domain_label": detail.domain_label,
        "origin": detail.origin,
        "options": [
            {
                "label": option.label,
                "text_md": option.text_md,
                "explanation_md": option.explanation_md,
                "is_correct": option.is_correct,
            }
            for option in detail.options
        ],
    }


def shuffled_labels(snapshot: dict, seed: int, position: int) -> list[str]:
    """The order this item's options are displayed in.

    Derived from the attempt's seed and the item's position, so `start()` is a pure
    function of the seed -- the permutation is stored anyway, and re-deriving it is
    never the read path, but an exam that cannot be reconstructed from its own seed
    is one nobody can reason about when its numbers look wrong.

    Shuffling is unconditionally safe here: the plan measured 0 references to an
    option letter across all 5490 corpus fields, so no explanation can be made
    nonsense by moving B above A.
    """
    labels = [option["label"] for option in snapshot["options"]]
    random.Random(f"{seed}:{position}:{snapshot['question_id']}").shuffle(labels)
    return labels


# ------------------------------------------------------------------------ the lifecycle


def open_attempt(
    session: Session, certification_id: str | None = None
) -> journal.Attempt | None:
    """The attempt in progress, if there is one. At most one, by construction."""
    statement = sa.select(journal.Attempt).where(
        journal.Attempt.submitted_at.is_(None), journal.Attempt.abandoned_at.is_(None)
    )
    if certification_id:
        statement = statement.where(journal.Attempt.certification_id == certification_id)
    return session.scalars(statement.order_by(journal.Attempt.started_at.desc())).first()


def start(
    session: Session,
    *,
    certification_id: str,
    count: int | None = None,
    filters: SearchFilters | None = None,
    seed: int | None = None,
    exclude: Iterable[str] = (),
    now: datetime | None = None,
    draw: Draw | None = None,
) -> journal.Attempt:
    """Draw an exam and freeze it. One open attempt at a time.

    Refusing a second open attempt is what makes "resume" mean anything: with two,
    the runner has to ask which, and every later query has to decide what an
    abandoned-but-not-really attempt counts for. Abandon or submit the first.
    """
    existing = open_attempt(session, certification_id)
    if existing is not None:
        raise AttemptError(
            f"attempt {existing.id} on {certification_id} is still open; "
            "submit or abandon it before starting another"
        )

    now = now or utcnow()
    if draw is None:
        draw = sampler.sample(
            session,
            certification_id=certification_id,
            count=count,
            filters=filters,
            seed=seed,
            exclude=exclude,
        )
    if not draw.question_ids:
        raise AttemptError(
            f"nothing to sit: no questions match for {certification_id}"
        )

    attempt = journal.Attempt(
        certification_id=certification_id,
        started_at=now,
        seed=draw.seed,
        item_count=draw.count,
        requested_count=draw.requested,
        sampler_json=draw.sampler_json(),
        # 019. Recorded on the attempt, not recomputed later: the blueprint can be
        # re-transcribed and the corpus can grow, and neither may change the mix a
        # sitting from six months ago is reported as having had.
        weight_source=draw.weight_source,
        apportionment_json=draw.apportionment_json(),
        time_limit_seconds=sampler.DEFAULT_TIME_LIMIT_SECONDS,
    )
    session.add(attempt)
    session.flush()

    exposures = _prior_exposures(session, draw.question_ids)
    flagged = set(draw.flagged)

    for position, question_id in enumerate(draw.question_ids):
        detail = queries.question_detail(session, question_id)
        if detail is None:  # pragma: no cover -- the draw came from this projection
            raise AttemptError(f"question {question_id} vanished between draw and start")
        snapshot = snapshot_of(detail)
        if question_id in flagged:
            snapshot["flagged"] = "disputed"
        session.add(
            journal.AttemptItem(
                attempt_id=attempt.id,
                position=position,
                question_id=question_id,
                snapshot_json=snapshot,
                option_order=shuffled_labels(snapshot, attempt.seed, position),
                seen_ordinal=exposures.get(question_id, 0) + 1,
            )
        )
    session.flush()
    return attempt


def _prior_exposures(session: Session, question_ids: Sequence[str]) -> dict[str, int]:
    """How many times each question has been put in front of this person before.

    Counted over every attempt, abandoned ones included: seeing a question and then
    abandoning the exam is still having seen it. 020 reads `seen_ordinal` to tell a
    first exposure from a re-test, and `first_exposure_response` (005) is the view
    that applies the stricter rule for *scoring*.
    """
    if not question_ids:
        return {}
    rows = session.execute(
        sa.select(journal.AttemptItem.question_id, sa.func.count())
        .where(journal.AttemptItem.question_id.in_(list(question_ids)))
        .group_by(journal.AttemptItem.question_id)
    ).all()
    return {question_id: int(count) for question_id, count in rows}


def answer(
    session: Session,
    attempt_id: int,
    position: int,
    labels: Iterable[str],
    *,
    time_ms: int = 0,
    now: datetime | None = None,
) -> journal.AttemptItem:
    """Record a selection. Written now, not at submit.

    Answering the same item again is normal -- people change their minds -- and it
    overwrites `selected_labels` while counting the change. What it never touches is
    the snapshot or the option order.
    """
    attempt = session.get(journal.Attempt, attempt_id)
    if attempt is None:
        raise AttemptError(f"no attempt {attempt_id}")
    if not _is_open(attempt):
        raise AttemptError(f"attempt {attempt_id} is closed; it can no longer be answered")

    item = session.scalars(
        sa.select(journal.AttemptItem).where(
            journal.AttemptItem.attempt_id == attempt_id,
            journal.AttemptItem.position == position,
        )
    ).first()
    if item is None:
        raise AttemptError(f"attempt {attempt_id} has no item at position {position}")

    chosen = _validate(item, labels)
    if item.answered_at is not None and list(item.selected_labels or []) != chosen:
        item.change_count += 1
    item.selected_labels = chosen
    item.answered_at = now or utcnow()
    if item.first_shown_at is None:
        item.first_shown_at = item.answered_at
    item.time_ms += max(0, int(time_ms))
    session.flush()
    return item


def _validate(item: journal.AttemptItem, labels: Iterable[str]) -> list[str]:
    """Only labels this item actually offered, in the snapshot's own order.

    Normalising the order here means "A,B" and "B,A" are the same answer, so a
    change count measures a change of mind rather than a change of click order.
    """
    offered = [option["label"] for option in item.snapshot_json["options"]]
    chosen = list(dict.fromkeys(labels))
    unknown = [label for label in chosen if label not in offered]
    if unknown:
        raise AttemptError(
            f"{unknown} not offered on {item.question_id}; it has {offered}"
        )
    return [label for label in offered if label in chosen]


def mark_shown(
    session: Session, attempt_id: int, position: int, *, now: datetime | None = None
) -> journal.AttemptItem:
    """Record that an item reached the screen, for recency (012) and timing."""
    item = session.scalars(
        sa.select(journal.AttemptItem).where(
            journal.AttemptItem.attempt_id == attempt_id,
            journal.AttemptItem.position == position,
        )
    ).first()
    if item is None:
        raise AttemptError(f"attempt {attempt_id} has no item at position {position}")
    if item.first_shown_at is None:
        item.first_shown_at = now or utcnow()
        session.flush()
    return item


def submit(
    session: Session, attempt_id: int, *, now: datetime | None = None
) -> journal.Attempt:
    """Close the attempt. Grading is 014; nothing here writes `is_correct`."""
    attempt = _closable(session, attempt_id)
    now = now or utcnow()
    attempt.submitted_at = now
    attempt.elapsed_ms = int((now - attempt.started_at).total_seconds() * 1000)
    session.flush()
    return attempt


def abandon(
    session: Session, attempt_id: int, *, now: datetime | None = None
) -> journal.Attempt:
    """Walk away. The rows stay; no statistic counts them.

    `submitted_at` stays NULL because it would be a lie -- this was not submitted --
    and everything that needs "finished" reads both columns. That is the whole of
    migration 0003.
    """
    attempt = _closable(session, attempt_id)
    now = now or utcnow()
    attempt.abandoned_at = now
    attempt.elapsed_ms = int((now - attempt.started_at).total_seconds() * 1000)
    session.flush()
    return attempt


def _closable(session: Session, attempt_id: int) -> journal.Attempt:
    attempt = session.get(journal.Attempt, attempt_id)
    if attempt is None:
        raise AttemptError(f"no attempt {attempt_id}")
    if not _is_open(attempt):
        state = "submitted" if attempt.submitted_at else "abandoned"
        raise AttemptError(f"attempt {attempt_id} is already {state}")
    return attempt


# ---------------------------------------------------------------------------- reading


def view(session: Session, attempt_id: int) -> AttemptView | None:
    """The attempt as a page needs it, built entirely from the snapshots.

    Two statements, whatever the exam length. Nothing in here reads `question` --
    a resumed exam shows what was frozen, not what the corpus says today.
    """
    attempt = session.get(journal.Attempt, attempt_id)
    if attempt is None:
        return None

    items = session.scalars(
        sa.select(journal.AttemptItem)
        .where(journal.AttemptItem.attempt_id == attempt_id)
        .order_by(journal.AttemptItem.position)
    ).all()

    return AttemptView(
        id=attempt.id,
        certification_id=attempt.certification_id,
        started_at=attempt.started_at,
        submitted_at=attempt.submitted_at,
        abandoned_at=attempt.abandoned_at,
        seed=attempt.seed,
        time_limit_seconds=attempt.time_limit_seconds,
        items=tuple(item_view(item) for item in items),
    )


def item_view(item: journal.AttemptItem) -> ItemView:
    """One item, with its options in the order they were shown."""
    snapshot = item.snapshot_json
    by_label = {option["label"]: option for option in snapshot["options"]}
    return ItemView(
        position=item.position,
        question_id=item.question_id,
        prompt_md=snapshot["prompt_md"],
        options=tuple(
            OptionView(label=label, text_md=by_label[label]["text_md"])
            for label in item.option_order
            if label in by_label
        ),
        type=snapshot["type"],
        select_count=int(snapshot.get("select_count") or 1),
        domain_label=snapshot.get("domain_label"),
        selected=tuple(item.selected_labels or ()),
        answered=item.answered_at is not None,
        flagged=snapshot.get("flagged") is not None,
        time_ms=item.time_ms,
        change_count=item.change_count,
        seen_ordinal=item.seen_ordinal,
    )
