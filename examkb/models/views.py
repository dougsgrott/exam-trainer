"""The five read-only views, as Python objects to query against.

**Their SQL lives in the migration that creates them**, not here. A view is a
replaceable object: changing one ships a migration that drops and recreates it,
and a copy of the text in the application would be a second source of truth that
goes stale the day the two disagree. What lives here is the *shape* -- the names
and columns the rest of the app queries -- on its own `MetaData` so
`Base.metadata.create_all()` and Alembic autogenerate leave it alone.
`tests/test_schema.py` asserts every view below exists in a migrated database
with exactly these columns.

Why each one is a view rather than a column:

- **`question_verification`** -- the plan is explicit that `verification` is never
  a mutable column a re-run can silently overwrite. Precedence is
  `known_bad` > open dispute > `human_reviewed` > `ai_reviewed` > `unverified`.
- **`current_answer_key`** -- an override corrects a key for *future* grading
  without touching the stored key or any past grade.
- **`current_mark`** -- marks are append-only history; the UI wants the latest.
- **`attempt_score`** -- one place that knows how an attempt is totalled.
- **`first_exposure_response`** -- "first exposure" is defined once, in SQL, and
  used by every statistic. A re-seen question is not a fresh observation of the
  same knowledge, and each analytics module inventing its own definition is how
  two pages end up disagreeing about the same number.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Column, Float, Integer, JSON, MetaData, Table, Text

from examkb.models.base import UTCDateTime

VIEW_METADATA = MetaData()

question_verification = Table(
    "question_verification",
    VIEW_METADATA,
    Column("question_id", Text, primary_key=True),
    Column("verification", Text),
)

current_answer_key = Table(
    "current_answer_key",
    VIEW_METADATA,
    Column("question_id", Text, primary_key=True),
    Column("correct_labels", JSON),
    Column("override_id", Integer),
    Column("is_override", Boolean),
)

current_mark = Table(
    "current_mark",
    VIEW_METADATA,
    Column("question_id", Text, primary_key=True),
    Column("value", Text),
    Column("marked_at", UTCDateTime),
)

attempt_score = Table(
    "attempt_score",
    VIEW_METADATA,
    Column("attempt_id", Integer, primary_key=True),
    Column("certification_id", Text),
    Column("started_at", UTCDateTime),
    Column("submitted_at", UTCDateTime),
    Column("item_count", Integer),
    Column("answered_count", Integer),
    Column("correct_count", Integer),
    Column("credit_total", Float),
)

first_exposure_response = Table(
    "first_exposure_response",
    VIEW_METADATA,
    Column("attempt_item_id", Integer, primary_key=True),
    Column("attempt_id", Integer),
    Column("question_id", Text),
    Column("blueprint_node_id", Text),
    Column("is_correct", Boolean),
    Column("credit", Float),
    Column("answered_at", UTCDateTime),
)

VIEWS = (
    question_verification,
    current_answer_key,
    current_mark,
    attempt_score,
    first_exposure_response,
)

VIEW_NAMES = tuple(view.name for view in VIEWS)

TRIGGER_NAMES = (
    "attempt_item_snapshot_is_immutable",
    "chat_thread_requires_no_open_attempt",
)
