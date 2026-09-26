"""Marks: the first thing in this repo that cannot be regenerated.

`localStorage['kb-marks']` is what this replaces -- a flat
`{"<question id>": "known"}` object in one browser, with no timestamps and no
history. Clearing a mark there is `delete marks[id]`; the fact that you once knew
a question is simply gone.

Two rules follow, and everything in this module is one of them:

1. **Append, never overwrite.** Changing known -> unsure writes a second row, and
   clearing writes a `cleared` row rather than deleting anything. `current_mark`
   (the view 005 built) is what the UI reads; the table underneath it is the
   history that was missing. A `DELETE` in this file would be the bug.
2. **Importing is idempotent.** The browser blob is the user's real study state and
   they will run the import more than once -- after every export, probably while
   unsure whether the last one worked. A mark whose current value already matches
   is skipped, so the second run writes nothing and says so.

The importer takes whatever a person can actually get out of devtools, which is
usually not the bare value: `JSON.stringify(localStorage)` is the easy thing to
type, and its `kb-marks` entry is a JSON string inside a JSON object.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

from examkb.models import journal
from examkb.models.base import UTCDateTime, utcnow

# 005's vocabulary. `cleared` is a value, not an absence -- that is the point.
VALUES: tuple[str, ...] = ("known", "unsure", "flagged")
CLEARED = "cleared"
ALL_VALUES: tuple[str, ...] = (*VALUES, CLEARED)

SOURCE_UI = "ui"
SOURCE_IMPORT = "import"

# The key the browser stores under, and the wrappers an export can arrive in.
STORAGE_KEY = "kb-marks"
WRAPPER_KEYS = ("marks", "kb-marks", "kbMarks")


class MarkError(ValueError):
    """Bad input, with the value that was wrong named in the message."""


# --------------------------------------------------------------------------- reading


@dataclass(frozen=True)
class MarkState:
    """What a question is marked as now, if anything."""

    question_id: str
    value: str | None
    marked_at: datetime | None = None

    @property
    def marked(self) -> bool:
        return self.value is not None

    def is_(self, value: str) -> bool:
        return self.value == value


def _normalise(value: str | None) -> str | None:
    """A stored value as the UI thinks of it: `cleared` and `''` are both "no mark"."""
    if not value or value == CLEARED:
        return None
    return value


# `.columns()` is not decoration. A bare `text()` hands back whatever the driver
# produced, and SQLite produces a *string* for a timestamp -- so `marked_at` would
# be typed `datetime` and hold `'2026-04-02 03:04:05.000000'`, which only shows up
# the first time something tries to format it. Naming the types here runs the value
# back through `UTCDateTime`, the same decorator the ORM columns use.
_CURRENT_MARK = sa.text(
    "SELECT value, marked_at FROM current_mark WHERE question_id = :id"
).columns(value=sa.Text, marked_at=UTCDateTime())


def current(session: Session, question_id: str) -> MarkState:
    row = session.execute(_CURRENT_MARK, {"id": question_id}).first()
    if row is None:
        return MarkState(question_id=question_id, value=None)
    return MarkState(question_id=question_id, value=_normalise(row[0]), marked_at=row[1])


def current_many(session: Session, question_ids: Iterable[str]) -> dict[str, str]:
    """`{question id: value}` for the ones that are marked. Unmarked ids are absent.

    One statement for a whole page of results, because the list page is not
    allowed to grow a query per row.
    """
    ids = list(question_ids)
    if not ids:
        return {}
    statement = sa.text(
        "SELECT question_id, value FROM current_mark WHERE question_id IN :ids"
    ).bindparams(sa.bindparam("ids", expanding=True))
    return {
        row[0]: _normalise(row[1])
        for row in session.execute(statement, {"ids": ids})
        if _normalise(row[1]) is not None
    }


def history(session: Session, question_id: str) -> list[journal.Mark]:
    """Every mark ever written for a question, oldest first. The point of the table."""
    return list(
        session.scalars(
            sa.select(journal.Mark)
            .where(journal.Mark.question_id == question_id)
            .order_by(journal.Mark.created_at, journal.Mark.id)
        ).all()
    )


def counts(session: Session) -> dict[str, int]:
    """How many questions currently carry each value."""
    rows = session.execute(
        sa.text("SELECT value, count(*) FROM current_mark GROUP BY value")
    ).all()
    found = {value: 0 for value in VALUES}
    for value, count in rows:
        if value in found:
            found[value] = int(count)
    return found


# --------------------------------------------------------------------------- writing


def set_mark(
    session: Session,
    question_id: str,
    value: str,
    *,
    source: str = SOURCE_UI,
    at: datetime | None = None,
) -> journal.Mark:
    """Append a mark. Never updates, never deletes."""
    if value not in ALL_VALUES:
        raise MarkError(f"{value!r} is not a mark; expected one of {', '.join(ALL_VALUES)}")
    mark = journal.Mark(
        question_id=question_id,
        value=value,
        source=source,
        created_at=at or utcnow(),
    )
    session.add(mark)
    session.flush()
    return mark


def clear_mark(session: Session, question_id: str, *, source: str = SOURCE_UI) -> journal.Mark:
    return set_mark(session, question_id, CLEARED, source=source)


def toggle(
    session: Session, question_id: str, value: str, *, source: str = SOURCE_UI
) -> MarkState:
    """Clicking the value a question already has clears it, as the browser did."""
    if value not in VALUES:
        raise MarkError(f"{value!r} is not a mark; expected one of {', '.join(VALUES)}")
    if current(session, question_id).is_(value):
        clear_mark(session, question_id, source=source)
    else:
        set_mark(session, question_id, value, source=source)
    return current(session, question_id)


# -------------------------------------------------------------------------- importing


@dataclass(frozen=True)
class ImportResult:
    imported: int = 0
    skipped: int = 0
    """Already at this value. The second run of the same blob is all of these."""

    unmatched: list[str] = field(default_factory=list)
    """Marked ids with no question in the corpus. Reported, never dropped silently."""

    ignored: dict[str, str] = field(default_factory=dict)
    """`{id: value}` for values outside the vocabulary."""

    total: int = 0

    def summary(self) -> str:
        lines = [
            f"import-marks: {self.total} mark(s) read -- "
            f"{self.imported} imported, {self.skipped} already current"
        ]
        if self.unmatched:
            shown = ", ".join(self.unmatched[:5])
            more = f" and {len(self.unmatched) - 5} more" if len(self.unmatched) > 5 else ""
            lines.append(f"  {len(self.unmatched)} not in the corpus: {shown}{more}")
        if self.ignored:
            lines.append(f"  {len(self.ignored)} unrecognised value(s): {sorted(set(self.ignored.values()))}")
        if not self.imported and not self.unmatched and not self.ignored:
            lines.append("  nothing to do; the database already matches")
        return "\n".join(lines)


def parse_blob(raw: str | bytes | Mapping) -> dict[str, str]:
    """Whatever came out of the browser -> `{question id: value}`.

    Four shapes, because there are four plausible ways to get this out of devtools
    and only one of them is the bare value:

    - `{"ccao-f/exam-01/q001": "known"}`   -- `localStorage.getItem('kb-marks')`
    - `{"kb-marks": "{...}"}`              -- `JSON.stringify(localStorage)`, the
                                              easy one, where the value is a *string*
    - `{"marks": {...}}`                   -- a hand-written wrapper
    - `[{"question_id": ..., "value": ...}]` -- what this tool would export
    """
    if isinstance(raw, (str, bytes)):
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise MarkError(f"not JSON: {error}") from error
    else:
        data = raw

    if isinstance(data, list):
        found: dict[str, str] = {}
        for entry in data:
            if not isinstance(entry, Mapping):
                raise MarkError(f"expected objects in the list, got {type(entry).__name__}")
            key = entry.get("question_id") or entry.get("id")
            if key:
                found[str(key)] = str(entry.get("value") or "")
        return found

    if not isinstance(data, Mapping):
        raise MarkError(f"expected an object of marks, got {type(data).__name__}")

    for key in WRAPPER_KEYS:
        if key in data:
            inner = data[key]
            # A localStorage dump stores every value as a string, so the marks
            # arrive as JSON inside JSON. Unwrap once, not recursively.
            if isinstance(inner, str):
                try:
                    inner = json.loads(inner)
                except json.JSONDecodeError as error:
                    raise MarkError(f"{key} is not JSON: {error}") from error
            if isinstance(inner, (Mapping, list)):
                return parse_blob(inner)

    return {str(key): str(value or "") for key, value in data.items()}


def import_marks(
    session: Session,
    raw: str | bytes | Mapping,
    *,
    at: datetime | None = None,
    source: str = SOURCE_IMPORT,
) -> ImportResult:
    """Import a `kb-marks` blob. Safe to run twice; the second run writes nothing.

    Unmatched ids are collected rather than raised: a corpus that has moved on is
    the normal case, not an error, and a half-finished import would be worse than
    a report.
    """
    marks = parse_blob(raw)
    at = at or utcnow()

    known_ids = {
        row[0]
        for row in session.execute(sa.text("SELECT id FROM question")).all()
    }
    existing = current_many(session, marks.keys())

    imported = skipped = 0
    unmatched: list[str] = []
    ignored: dict[str, str] = {}

    for question_id, value in sorted(marks.items()):
        if not value:
            # The browser deletes cleared marks, so an empty value is "not marked"
            # and importing it would write a `cleared` row for something that was
            # never marked here.
            continue
        if value not in VALUES:
            ignored[question_id] = value
            continue
        if question_id not in known_ids:
            unmatched.append(question_id)
            continue
        if existing.get(question_id) == value:
            skipped += 1
            continue
        set_mark(session, question_id, value, source=source, at=at)
        imported += 1

    return ImportResult(
        imported=imported,
        skipped=skipped,
        unmatched=unmatched,
        ignored=ignored,
        total=len([value for value in marks.values() if value]),
    )


def read_blob(path: Path | str) -> tuple[str, datetime | None]:
    """The file's text and its mtime.

    The mtime is used as the mark timestamp, because the blob has none of its own
    and "when the browser last saved it" is a better answer than "when you got
    round to importing it". `--at` overrides it.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    try:
        stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:  # pragma: no cover -- it was just read
        stamp = None
    return text, stamp


# ------------------------------------------------------- the web layer's entry points
#
# `examkb/web/` may not open a session (008's layering rule), so the two things a
# page does -- read a mark, toggle one -- own their transaction here. Everything
# above takes a `Session` and is what the CLI, the tests and 012's sampler use.


def apply_toggle(
    question_id: str, value: str, *, url: str | None = None, source: str = SOURCE_UI
) -> MarkState:
    """Toggle and commit. The state afterwards is what the fragment renders."""
    from examkb import db

    with db.session_for(url) as session:
        state = toggle(session, question_id, value, source=source)
        session.commit()
        return state


def state_of(question_id: str, *, url: str | None = None) -> MarkState:
    from examkb import db

    with db.session_for(url) as session:
        return current(session, question_id)


def summary(*, url: str | None = None) -> dict[str, int]:
    from examkb import db

    with db.session_for(url) as session:
        return counts(session)
