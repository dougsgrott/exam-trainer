"""The exam runner's session seam, and the shape a page renders.

`examkb/web/` may not open a database session (008's layering rule), so this is
to `/exam` what `services/browse.py` is to `/browse`: it owns the transaction and
hands back frozen dataclasses. 013's `attempts` module is the lifecycle underneath
and takes a `Session`; nothing here re-implements it.

Two things are decided here rather than in the route, because they are about the
exam and not about HTTP:

**The timer is the server's.** `remaining_seconds` is computed from
`attempt.started_at` and the recorded limit, every time anything is asked. The
client counts down for display; it is never asked what time it is, so there is
nothing for a client clock to tamper with.

**The key never leaves.** `RunnerItem` carries the prompt and the options and
nothing else, even though the snapshot it is built from holds `correct_labels`.
The runner cannot leak an answer it was never given.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import ceil

from examkb import db
from examkb.services import attempts, marks
from examkb.services.attempts import AttemptError, AttemptView, ItemView

from examkb.services.search import SearchFilters


@dataclass(frozen=True)
class RunnerOption:
    label: str
    text_md: str
    selected: bool


@dataclass(frozen=True)
class RunnerItem:
    """One question as the runner shows it. No key, no explanations."""

    position: int
    question_id: str
    prompt_md: str
    options: tuple[RunnerOption, ...]
    multi: bool
    select_count: int
    domain_label: str | None
    answered: bool
    flagged: bool
    disputed: bool

    @property
    def number(self) -> int:
        return self.position + 1


@dataclass(frozen=True)
class MapEntry:
    position: int
    answered: bool
    flagged: bool
    current: bool

    @property
    def number(self) -> int:
        return self.position + 1

    @property
    def state(self) -> str:
        return "current" if self.current else ("answered" if self.answered else "open")


@dataclass(frozen=True)
class RunnerPage:
    attempt_id: int
    certification_id: str
    item: RunnerItem
    map: tuple[MapEntry, ...]
    answered_count: int
    count: int
    remaining_seconds: int | None
    open: bool

    @property
    def unanswered(self) -> int:
        return self.count - self.answered_count

    @property
    def previous(self) -> int | None:
        return self.item.position - 1 if self.item.position > 0 else None

    @property
    def next(self) -> int | None:
        return self.item.position + 1 if self.item.position + 1 < self.count else None

    @property
    def remaining_label(self) -> str:
        """`h:mm:ss` for the first render; the script re-formats from the number."""
        if self.remaining_seconds is None:
            return ""
        total = max(0, self.remaining_seconds)
        return f"{total // 3600}:{total // 60 % 60:02d}:{total % 60:02d}"

    @property
    def out_of_time(self) -> bool:
        return self.remaining_seconds is not None and self.remaining_seconds <= 0


@dataclass(frozen=True)
class StartPage:
    """`/exam` before there is anything to sit."""

    certifications: tuple[tuple[str, str, int], ...]
    """`(id, name, questions available)`."""

    open_attempt_id: int | None = None
    open_certification_id: str | None = None


def remaining_seconds(attempt: AttemptView | None, now: datetime | None = None) -> int | None:
    """Seconds left, from the server's clock and the attempt's own start time."""
    if attempt is None or attempt.time_limit_seconds is None:
        return None
    from examkb.models.base import utcnow

    elapsed = ((now or utcnow()) - attempt.started_at).total_seconds()
    return max(0, int(ceil(attempt.time_limit_seconds - elapsed)))


def _runner_item(item: ItemView, flagged: bool) -> RunnerItem:
    return RunnerItem(
        position=item.position,
        question_id=item.question_id,
        prompt_md=item.prompt_md,
        options=tuple(
            RunnerOption(label=option.label, text_md=option.text_md,
                         selected=option.label in item.selected)
            for option in item.options
        ),
        multi=item.multi,
        select_count=item.select_count,
        domain_label=item.domain_label,
        answered=item.answered,
        flagged=flagged,
        disputed=item.flagged,
    )


# ------------------------------------------------------------------------- reading


def start_page(*, url: str | None = None) -> StartPage:
    """What `/exam` offers: the certifications with questions, and any open sitting."""
    import sqlalchemy as sa

    with db.session_for(url) as session:
        rows = session.execute(
            sa.text(
                "SELECT certification.id, certification.name, count(question.id) "
                "FROM certification LEFT JOIN question "
                "  ON question.certification_id = certification.id "
                "GROUP BY certification.id ORDER BY certification.name"
            )
        ).all()
        current = attempts.open_attempt(session)
        return StartPage(
            certifications=tuple((row[0], row[1], int(row[2])) for row in rows),
            open_attempt_id=current.id if current else None,
            open_certification_id=current.certification_id if current else None,
        )


def page(
    attempt_id: int, position: int, *, url: str | None = None, now: datetime | None = None
) -> RunnerPage | None:
    """One question of one attempt, plus the map and the clock. Two statements."""
    with db.session_for(url) as session:
        view = attempts.view(session, attempt_id)
        if view is None or not view.items:
            return None
        position = max(0, min(position, len(view.items) - 1))

        flagged = marks.current_many(session, [item.question_id for item in view.items])
        item = view.items[position]

        return RunnerPage(
            attempt_id=view.id,
            certification_id=view.certification_id,
            item=_runner_item(item, flagged.get(item.question_id) == "flagged"),
            map=tuple(
                MapEntry(
                    position=other.position,
                    answered=other.answered,
                    flagged=flagged.get(other.question_id) == "flagged",
                    current=other.position == position,
                )
                for other in view.items
            ),
            answered_count=view.answered_count,
            count=view.count,
            remaining_seconds=remaining_seconds(view, now),
            open=view.open,
        )


def first_unanswered(attempt_id: int, *, url: str | None = None) -> int:
    """Where to drop somebody who just opened the exam again."""
    with db.session_for(url) as session:
        view = attempts.view(session, attempt_id)
        if view is None:
            return 0
        for item in view.items:
            if not item.answered:
                return item.position
        return 0


# ------------------------------------------------------------------------- writing


@dataclass(frozen=True)
class SaveResult:
    """What a 204's `HX-Trigger` carries back: enough to re-sync the page."""

    attempt_id: int
    position: int
    selected: tuple[str, ...]
    answered_count: int
    count: int
    remaining_seconds: int | None
    flagged: bool = False

    def payload(self) -> dict:
        return {
            "attemptId": self.attempt_id,
            "position": self.position,
            "selected": list(self.selected),
            "answered": self.answered_count,
            "count": self.count,
            "remaining": self.remaining_seconds,
            "flagged": self.flagged,
        }


def begin(
    *,
    certification_id: str,
    count: int | None = None,
    seed: int | None = None,
    filters: SearchFilters | None = None,
    url: str | None = None,
    now: datetime | None = None,
) -> int:
    """Start a sitting and return its id. Refuses if one is already open."""
    with db.session_for(url) as session:
        attempt = attempts.start(
            session,
            certification_id=certification_id,
            count=count,
            seed=seed,
            filters=filters,
            now=now,
        )
        session.commit()
        return attempt.id


def save_answer(
    attempt_id: int,
    position: int,
    labels,
    *,
    time_ms: int = 0,
    url: str | None = None,
    now: datetime | None = None,
) -> SaveResult:
    """Record one selection and commit it.

    The position is an argument, never "whichever question the server thinks you
    are on". A save that leaves the browser before a navigation and arrives after
    it still lands on the question it was made against -- there is no server-side
    cursor for it to race.
    """
    with db.session_for(url) as session:
        item = attempts.answer(session, attempt_id, position, labels, time_ms=time_ms, now=now)
        view = attempts.view(session, attempt_id)
        session.commit()
        return SaveResult(
            attempt_id=attempt_id,
            position=position,
            selected=tuple(item.selected_labels or ()),
            answered_count=view.answered_count,
            count=view.count,
            remaining_seconds=remaining_seconds(view, now),
        )


def toggle_flag(
    attempt_id: int, position: int, *, url: str | None = None, now: datetime | None = None
) -> SaveResult:
    """Flag a question for review.

    Deliberately the same `flagged` mark `/browse` filters on (011), not a new
    per-attempt column. "Come back to this" is one fact whether it was noticed
    during an exam or while reading, and keeping it in one place means the
    after-the-exam review list is `/browse?mark=flagged` rather than a new page.
    """
    with db.session_for(url) as session:
        view = attempts.view(session, attempt_id)
        if view is None or position >= len(view.items):
            raise AttemptError(f"attempt {attempt_id} has no item at position {position}")
        question_id = view.items[position].question_id
        state = marks.toggle(session, question_id, "flagged")
        session.commit()
        return SaveResult(
            attempt_id=attempt_id,
            position=position,
            selected=tuple(view.items[position].selected),
            answered_count=view.answered_count,
            count=view.count,
            remaining_seconds=remaining_seconds(view, now),
            flagged=state.value == "flagged",
        )


def finish(attempt_id: int, *, url: str | None = None, now: datetime | None = None) -> int:
    with db.session_for(url) as session:
        attempt = attempts.submit(session, attempt_id, now=now)
        session.commit()
        return attempt.id


def walk_away(attempt_id: int, *, url: str | None = None, now: datetime | None = None) -> int:
    with db.session_for(url) as session:
        attempt = attempts.abandon(session, attempt_id, now=now)
        session.commit()
        return attempt.id
