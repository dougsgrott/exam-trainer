"""The schema, and the check that keeps its two halves apart.

Importing this module imports both halves and then verifies the split: a
`Journal` subclass defined in `corpus.py`, or a `Projection` subclass defined in
`journal.py`, raises here rather than being caught in review. The rule is
load-bearing -- 006 rebuilds every PROJECTION table from `kb/` and must never
touch a JOURNAL one -- so it is enforced where it cannot be forgotten.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Table

from examkb.models import corpus, journal
from examkb.models.base import (
    JOURNAL,
    PROJECTION,
    Base,
    Journal,
    Projection,
    UTCDateTime,
    utcnow,
)

MODULE_FOR_CLASS = {
    PROJECTION: corpus.__name__,
    JOURNAL: journal.__name__,
}


def enforce_table_class_split(classes: Iterable[type] | None = None) -> None:
    """Raise unless every mapped class lives in the module for its table class.

    Takes the classes to check so the rule itself is testable without mutating the
    registry: `tests/test_schema.py` hands it a deliberately misplaced class and
    asserts it raises.
    """
    if classes is None:
        classes = [mapper.class_ for mapper in Base.registry.mappers]
    for cls in classes:
        table_class = getattr(cls, "table_class", None)
        expected = MODULE_FOR_CLASS.get(table_class)
        if expected is None:
            raise RuntimeError(
                f"{cls.__name__} inherits from neither Projection nor Journal; "
                "every table belongs to exactly one class"
            )
        if cls.__module__ != expected:
            raise RuntimeError(
                f"{cls.__name__} is a {table_class} table but is defined in "
                f"{cls.__module__}; it belongs in {expected}"
            )


def mapped_classes(table_class: str) -> list[type]:
    """Every mapped class of one table class, in table-name order."""
    return sorted(
        (m.class_ for m in Base.registry.mappers if m.class_.table_class == table_class),
        key=lambda cls: cls.__tablename__,
    )


def tables(table_class: str) -> list[Table]:
    """Every `Table` of one table class, in table-name order."""
    return [cls.__table__ for cls in mapped_classes(table_class)]


def projection_tables() -> list[Table]:
    return tables(PROJECTION)


def journal_tables() -> list[Table]:
    return tables(JOURNAL)


enforce_table_class_split()

metadata = Base.metadata

__all__ = [
    "Base",
    "enforce_table_class_split",
    "JOURNAL",
    "Journal",
    "PROJECTION",
    "Projection",
    "UTCDateTime",
    "corpus",
    "journal",
    "journal_tables",
    "mapped_classes",
    "metadata",
    "projection_tables",
    "tables",
    "utcnow",
]
