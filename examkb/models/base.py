"""The declarative base, the table-class split, and the two shared column types.

**The table-class split is structural, not documentary.** Every mapped class
inherits from `Projection` or `Journal`, and `examkb.models.__init__` refuses to
import if one of them is defined outside its own module. The rule it enforces is
the plan's:

- **PROJECTION** (`examkb/models/corpus.py`) is a pure function of `kb/` bytes.
  Every key is derived from content, so `ingest --rebuild` reproduces it; ingest
  may truncate and rewrite the whole class at will.
- **JOURNAL** (`examkb/models/journal.py`) is attempts, marks, disputes, chat,
  jobs and candidates. Ingest never touches it, and nothing in it is derivable
  from `kb/`. It is the only unregenerable thing in this repo.

The one consequence worth stating out loud: **a JOURNAL table never carries a
foreign key into a PROJECTION table.** A question that disappears from `kb/`
disappears from the projection, and the attempt that was sat on it is still
history -- so journal rows hold a plain `question_id` string with an index and no
referential action. `tests/test_schema.py` asserts it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

# Named constraints, so a later SQLite ALTER (which rebuilds the table) can address
# them. Without this every CHECK and FK is anonymous and `batch_alter_table` guesses.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

PROJECTION = "PROJECTION"
JOURNAL = "JOURNAL"


class UTCDateTime(TypeDecorator):
    """A datetime that is always UTC and always aware, on both sides of the wire.

    SQLite has no datetime type and SQLAlchemy's `DateTime` will happily store a
    naive local timestamp next to an aware UTC one. Every timestamp in this schema
    feeds a duration, a staleness window or an ordering, so the normalisation
    happens once, here, rather than in thirteen services.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"naive datetime {value!r}: timestamps must be timezone-aware")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    """Now, aware and in UTC. The only default for a timestamp column."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Projection(Base):
    """A row that is a pure function of `kb/` bytes. Lives in `corpus.py`."""

    __abstract__ = True

    table_class = PROJECTION

    content_keyed = True
    """True when the primary key is derived from `kb/` content.

    Nothing sets it False today -- even `ingest_run`, whose key is the corpus
    fingerprint itself. It stays as the declaration a future projection table
    would have to make, and `tests/test_schema.py` asserts the list of exemptions
    is empty: a key `ingest --rebuild` cannot reproduce is a key that breaks the
    projection's one promise.
    """


class Journal(Base):
    """A row nothing can regenerate. Lives in `journal.py`."""

    __abstract__ = True

    table_class = JOURNAL
